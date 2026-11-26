"""The dispatcher tick (plan §7.3): the thin shell around `optimizer.guard_rails`.

It runs right after every log ingestion (so the kill switch acts within one tick, 60 s by default, of the
user's activity) and after a pause or resume from the API. When the user has turned automatic runs on
(ADR 0017) and the guard rails allow a start, it selects a task (ADR 0016) and starts it with the plan's
`allowed_now` as its budget.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from surplus import preferences
from surplus.clock import Clock
from surplus.collector.service import Collector
from surplus.db.models import RunRow, SettingRow
from surplus.domain.events import Event, EventBus
from surplus.inbox import Inbox
from surplus.optimizer import control
from surplus.optimizer.guard_rails import (
    BREAKER_FAILURES,
    ActiveRun,
    FinishedRun,
    GuardDecision,
    breaker_trips,
    guard,
)
from surplus.optimizer.selection import Candidate, select_task
from surplus.planning import PlanService
from surplus.predictor.activity import Activity
from surplus.projects import repo
from surplus.projects.service import ProjectService
from surplus.runner.service import PreflightError, RunActionError, RunService, run_name
from surplus.runner.state import RunState

log = structlog.get_logger(__name__)
BREAKER_KEY = "breaker:{}"
AUTO_BACKOFF = timedelta(minutes=30)  # a task whose automatic start was refused is not retried before this
FINISHED = frozenset({RunState.AWAITING_FEEDBACK, RunState.DONE, RunState.FAILED})


@dataclass
class DispatcherStatus:
    at: datetime | None = None
    activity: Activity | None = None
    pause: control.PauseState | None = None
    decision: GuardDecision | None = None
    tripped: list[str] = field(default_factory=list)  # projects paused by the breaker in this process
    auto_enabled: bool = False


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
        planner: PlanService | None = None,
        inbox: Inbox | None = None,
    ) -> None:
        self.sm, self.clock, self.bus = sm, clock, bus
        self.planner = planner
        self.inbox = inbox
        self._backoff: dict[tuple[str, str], datetime] = {}
        self.collector, self.runs, self.projects = collector, runs, projects
        self.max_concurrent = max_concurrent
        self.status = DispatcherStatus()
        self._last_reason: str | None = None

    async def tick(self, allow_start: bool = False) -> GuardDecision:
        """Apply the guard rails. Only the regular loop passes `allow_start` (via `loop_tick`): a page load
        or a pause/resume never starts a run."""
        now = self.clock.now()
        activity = self.collector.activity()
        with self.sm() as session:
            pause = control.get_pause(session, now)
            enabled = preferences.auto_runs_enabled(session)
        active = [ActiveRun(r.id, r.trigger) for r in self.runs.active_runs()]
        fresh = self.runs.resets_fresh()
        allowed: float | None = None
        possible = enabled and pause is None and not activity.active and not active and fresh
        if possible and self.planner:
            # Only plan when a start is possible: the plan reads weeks of history.
            live = await asyncio.to_thread(self.planner.current)
            allowed = live.plan.allowed_now if live.plan else None
        decision = guard(
            activity,
            pause.describe() if pause else None,
            active,
            allowed_now=allowed,
            resets_fresh=fresh,
            max_concurrent=self.max_concurrent,
            auto_enabled=enabled,
        )
        for stop in decision.stops:
            try:
                await self.runs.stop(stop.run_id, stop.reason, stop.detail)
                log.info("dispatcher.stop", run=stop.run_id, reason=stop.reason.value, detail=stop.detail)
            except RunActionError:
                pass  # it ended or is already wrapping up
        self._check_breakers()
        started: RunRow | None = None
        if allow_start and decision.auto_start and allowed is not None:
            started, why = await self._start_auto(allowed)
            decision = GuardDecision(decision.stops, decision.auto_start, f"{decision.reason}; {why}")
        changed = decision.reason != self._last_reason or bool(decision.stops) or started is not None
        self.status = DispatcherStatus(now, activity, pause, decision, self.status.tripped, enabled)
        if changed:
            self._last_reason = decision.reason
            log.info(
                "dispatcher.decision",
                reason=decision.reason,
                auto_enabled=enabled,
                auto_start=decision.auto_start,
                allowed_now=allowed,
                user_active=activity.active,
                activity=activity.reason,
                paused=pause is not None,
                active_runs=[r.id for r in active],
                stops=[s.run_id for s in decision.stops],
                started=started.id if started else None,
            )
            self.bus.publish(Event("dispatcher.updated", {"reason": decision.reason}))
        return decision

    async def loop_tick(self) -> GuardDecision:
        return await self.tick(allow_start=True)

    # ------------------------------------------------------------------ automatic runs (ADR 0017)

    def candidates(self) -> list[Candidate]:
        """Every open task of every active project, with what selection needs to know about it."""
        with self.sm() as session:
            waiting = set(
                session.execute(
                    select(RunRow.project_id, RunRow.task_id).where(
                        RunRow.state == RunState.AWAITING_FEEDBACK.value
                    )
                ).all()
            )
            last_run = {
                (p, t): ts
                for p, t, ts in session.execute(
                    select(RunRow.project_id, RunRow.task_id, func.max(RunRow.created_at)).group_by(
                        RunRow.project_id, RunRow.task_id
                    )
                ).all()
            }
        questions = self.inbox.by_task() if self.inbox else {}
        out: list[Candidate] = []
        for prow in self.projects.registry.rows(include_missing=False):
            if prow.status != repo.ProjectStatus.ACTIVE.value:
                continue
            try:
                snap = self.projects.snapshot(prow.id)
            except Exception:
                log.warning("dispatcher.snapshot_failed", project=prow.id)
                continue
            if snap.meta is None or snap.backlog is None or snap.meta.status is not repo.ProjectStatus.ACTIVE:
                continue
            for pos, item in enumerate(snap.backlog.items):
                if item.done or item.proposed or "blocked" in item.flags:
                    continue
                key = (prow.id, item.id)
                q = questions.get(key)
                out.append(
                    Candidate(
                        project_id=prow.id,
                        project_priority=snap.meta.priority,
                        max_share=snap.meta.max_share_of_surplus,
                        task_id=item.id,
                        task_priority=item.priority,
                        position=pos,
                        size=item.size.value if item.size else None,
                        awaiting_review=(prow.id, item.id) in waiting,
                        question_pending=bool(q and q.pending),
                        answered=bool(
                            q
                            and q.answered_at
                            and (last_run.get(key) is None or q.answered_at >= last_run[key])
                        ),
                    )
                )
        return out

    def spend_7d(self) -> dict[str, float]:
        since = self.clock.now() - timedelta(days=7)
        with self.sm() as session:
            rows = session.execute(
                select(RunRow.project_id, func.sum(RunRow.spent_pct_est))
                .where(RunRow.created_at >= since)
                .group_by(RunRow.project_id)
            ).all()
        return {p: float(v or 0.0) for p, v in rows}

    async def _start_auto(self, allowed: float) -> tuple[RunRow | None, str]:
        now = self.clock.now()
        self._backoff = {k: t for k, t in self._backoff.items() if t > now}
        cands = [c for c in self.candidates() if (c.project_id, c.task_id) not in self._backoff]
        sel = select_task(cands, allowed, self.spend_7d())
        if sel.choice is None:
            return None, "nothing to run" + (
                f" ({sel.skipped[0][0]}: {sel.skipped[0][1]})" if sel.skipped else ""
            )
        c = sel.choice
        try:
            row = await self.runs.start(
                c.project_id, c.task_id, trigger="auto", budget=allowed, mode=c.mode.value
            )
        except PreflightError as exc:
            self._backoff[(c.project_id, c.task_id)] = now + AUTO_BACKOFF
            log.warning(
                "dispatcher.auto_start_refused", project=c.project_id, task=c.task_id, problems=exc.problems
            )
            return None, f"{c.project_id}/{c.task_id} could not start: {exc}"
        log.info(
            "dispatcher.auto_started",
            run=row.id,
            project=c.project_id,
            task=c.task_id,
            mode=c.mode.value,
            budget=allowed,
            why=c.reason,
            skipped=[f"{n}: {w}" for n, w in sel.skipped],
        )
        self.bus.publish(Event("run.changed", {"id": row.id}))
        return row, f"started {run_name(row.id)} on {c.project_id}/{c.task_id} ({c.reason})"

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

    # ------------------------------------------------------------------ pause, resume, opt-in

    async def set_auto(self, enabled: bool) -> bool:
        with self.sm.begin() as session:
            preferences.set_auto_runs(session, enabled)
        log.info("dispatcher.auto_runs", enabled=enabled)
        await self.tick()
        return enabled

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
