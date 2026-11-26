"""The dispatcher tick (plan §7.3): the thin shell around `optimizer.guard_rails`.

It runs right after every log ingestion (so the kill switch acts within one tick, 60 s by default, of the
user's activity) and after a pause or resume from the API. Runs stay manual in Phase 4: the decision whether
an automatic run *may* start is computed and shown, but nothing is started (ADR 0014).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import Clock
from surplus.collector.service import Collector
from surplus.db.models import RunRow, SettingRow
from surplus.domain.events import Event, EventBus
from surplus.optimizer import control
from surplus.optimizer.guard_rails import (
    BREAKER_FAILURES,
    ActiveRun,
    FinishedRun,
    GuardDecision,
    breaker_trips,
    guard,
)
from surplus.predictor.activity import Activity
from surplus.projects import repo
from surplus.projects.service import ProjectService
from surplus.runner.service import RunActionError, RunService
from surplus.runner.state import RunState

log = structlog.get_logger(__name__)
BREAKER_KEY = "breaker:{}"
FINISHED = frozenset({RunState.AWAITING_FEEDBACK, RunState.DONE, RunState.FAILED})


@dataclass
class DispatcherStatus:
    at: datetime | None = None
    activity: Activity | None = None
    pause: control.PauseState | None = None
    decision: GuardDecision | None = None
    tripped: list[str] = field(default_factory=list)  # projects paused by the breaker in this process


class Dispatcher:
    def __init__(
        self,
        sm: sessionmaker[Session],
        clock: Clock,
        bus: EventBus,
        collector: Collector,
        runs: RunService,
        projects: ProjectService,
        max_concurrent: int,
    ) -> None:
        self.sm, self.clock, self.bus = sm, clock, bus
        self.collector, self.runs, self.projects = collector, runs, projects
        self.max_concurrent = max_concurrent
        self.status = DispatcherStatus()
        self._last_reason: str | None = None

    async def tick(self) -> GuardDecision:
        now = self.clock.now()
        activity = self.collector.activity()
        with self.sm() as session:
            pause = control.get_pause(session, now)
        active = [ActiveRun(r.id, r.trigger) for r in self.runs.active_runs()]
        decision = guard(
            activity,
            pause.describe() if pause else None,
            active,
            allowed_now=None,  # Phase 5: the planner's allowed_now; no automatic runs before that
            resets_fresh=self.runs.resets_fresh(),
            max_concurrent=self.max_concurrent,
        )
        for stop in decision.stops:
            try:
                await self.runs.stop(stop.run_id, stop.reason, stop.detail)
                log.info("dispatcher.stop", run=stop.run_id, reason=stop.reason.value, detail=stop.detail)
            except RunActionError:
                pass  # it ended or is already wrapping up
        self._check_breakers()
        changed = decision.reason != self._last_reason or bool(decision.stops)
        self.status = DispatcherStatus(now, activity, pause, decision, self.status.tripped)
        if changed:
            self._last_reason = decision.reason
            log.info(
                "dispatcher.decision",
                reason=decision.reason,
                auto_start=decision.auto_start,
                user_active=activity.active,
                activity=activity.reason,
                paused=pause is not None,
                active_runs=[r.id for r in active],
                stops=[s.run_id for s in decision.stops],
            )
            self.bus.publish(Event("dispatcher.updated", {"reason": decision.reason}))
        return decision

    def _check_breakers(self) -> None:
        with self.sm() as session:
            rows = session.execute(
                select(RunRow.id, RunRow.project_id, RunRow.state)
                .where(RunRow.state.in_([s.value for s in FINISHED]))
                .order_by(RunRow.id.desc())
                .limit(200)
            ).all()
            settings = {
                r.key: r.value_json
                for r in session.scalars(select(SettingRow).where(SettingRow.key.like("breaker:%")))
            }
        by_project: dict[str, list[FinishedRun]] = {}
        for run_id, project_id, state in rows:
            runs = by_project.setdefault(project_id, [])
            if len(runs) < BREAKER_FAILURES:
                runs.append(FinishedRun(run_id, state == RunState.FAILED.value))
        for project_id, recent in by_project.items():
            key = BREAKER_KEY.format(project_id)
            last = json.loads(settings[key]) if key in settings else None
            if breaker_trips(recent, last):
                self._trip(project_id, max(r.id for r in recent))

    def _trip(self, project_id: str, newest_run: int) -> None:
        with self.sm.begin() as session:
            key = BREAKER_KEY.format(project_id)
            row = session.get(SettingRow, key)
            if row is None:
                session.add(SettingRow(key=key, value_json=json.dumps(newest_run)))
            else:
                row.value_json = json.dumps(newest_run)
        try:
            folder = self.projects.folder(project_id)
            _, h = repo.ProjectFiles(folder).read(repo.DocName.PROJECT)
            self.projects.update_settings(project_id, {"status": repo.ProjectStatus.PAUSED.value}, h)
        except Exception:
            log.exception("dispatcher.breaker_pause_failed", project=project_id)
            return
        self.status.tripped.append(project_id)
        log.warning("dispatcher.breaker_tripped", project=project_id, failures=BREAKER_FAILURES)
        self.bus.publish(
            Event(
                "project.paused",
                {"project_id": project_id, "reason": f"{BREAKER_FAILURES} failed runs in a row"},
            )
        )

    # ------------------------------------------------------------------ pause and resume

    async def pause(self, duration: timedelta | None, reason: str = "") -> control.PauseState:
        with self.sm.begin() as session:
            state = control.set_pause(session, self.clock.now(), duration, reason)
        log.info("dispatcher.paused", until=str(state.until), indefinite=state.indefinite, reason=reason)
        await self.tick()
        return state

    async def resume(self) -> bool:
        with self.sm.begin() as session:
            had = control.clear_pause(session)
        log.info("dispatcher.resumed", had_pause=had)
        await self.tick()
        return had
