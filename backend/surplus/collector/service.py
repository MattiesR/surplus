"""Collector service: the I/O shell around the pure collector core.

Runs inside `surplusd`: ingests logs and the status-line spool every tick, runs the Reset Watcher's probe on
schedule, and answers the dashboard's queries.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import Clock
from surplus.collector import reset_watcher
from surplus.collector.sources.probe import ProbeError, run_probe
from surplus.collector.sources.statusline import ingest_spool
from surplus.collector.split import (
    SeriesPoint,
    Split,
    SurfacePoint,
    WeightedEvent,
    hourly_series,
    weekly_split,
    weighted_tokens,
)
from surplus.collector.store import (
    IngestStats,
    buckets_with_readings,
    cost_model_inputs,
    ingest_logs,
    insert_readings,
    insert_snapshot,
    last_manual_event_at,
    latest_cost_model,
    latest_cost_model_fit_at,
    latest_readings,
    latest_reset_reading,
    mark_source,
    observation_inputs,
    save_cost_model,
)
from surplus.config import Paths, Settings
from surplus.db.models import SourceStatusRow, SurfaceReadingRow, UsageEventRow
from surplus.domain.events import Event, EventBus
from surplus.domain.models import (
    FIVE_HOUR,
    SEVEN_DAY,
    LimitReading,
    ReadingSource,
    SurfaceShare,
    TokenCounts,
    UsageSnapshot,
)
from surplus.predictor.activity import RECENT_JUMP, Activity, ReadingRise, user_activity
from surplus.predictor.baseline import SeasonalBaseline, SlotObservation, fit_baseline
from surplus.predictor.cost_model import CostModelFit, build_windows, estimate_pct, fit_cost_model
from surplus.predictor.observations import CostedEvent, user_slot_observations

log = structlog.get_logger(__name__)

ProbeFn = Callable[[], Awaitable[UsageSnapshot]]

# Cost model (plan §5.3): refit nightly on the last four weeks of windows.
COST_MODEL_REFIT_EVERY = timedelta(hours=24)
COST_MODEL_HISTORY = timedelta(days=28)
# Seasonal baseline (plan §6.1): fitted on 8 weeks (decay half-life 3 weeks), refitted hourly.
FORECAST_HISTORY = timedelta(weeks=8)
FORECAST_REFIT_EVERY = timedelta(hours=1)


class Collector:
    def __init__(
        self,
        sm: sessionmaker[Session],
        paths: Paths,
        settings: Settings,
        clock: Clock,
        bus: EventBus,
        probe: ProbeFn | None = None,
    ) -> None:
        self.sm, self.paths, self.settings, self.clock, self.bus = sm, paths, settings, clock, bus
        self._probe: ProbeFn = probe or (
            lambda: run_probe(settings.claude_bin, clock, settings.probe_timeout_seconds)
        )
        self.last_probe_at: datetime | None = None
        self._probe_lock = asyncio.Lock()
        # A fit without coefficients stores nothing; remember the attempt so it is not retried every tick.
        self.last_cost_fit_at: datetime | None = None
        self._baselines: dict[str, SeasonalBaseline] = {}

    # ------------------------------------------------------------------ ingestion

    def ingest_once(self) -> IngestStats:
        stats = ingest_logs(self.sm, self.paths.log_roots, self.clock)
        spooled = ingest_spool(self.sm, self.paths.statusline_spool, self.clock)
        if stats.events_inserted or spooled:
            self.bus.publish(Event("usage.updated", {"events": stats.events_inserted, "readings": spooled}))
        return stats

    async def probe_once(self) -> UsageSnapshot | None:
        async with self._probe_lock:
            now = self.clock.now()
            with self.sm() as session:
                before = reset_watcher.expectations(latest_readings(session))
            try:
                snap = await self._probe()
            except (ProbeError, OSError) as exc:
                log.warning("probe.failed", error=str(exc))
                with self.sm.begin() as session:
                    mark_source(session, "probe", now, str(exc))
                self.last_probe_at = now
                self.bus.publish(Event("source.error", {"source": "probe", "error": str(exc)}))
                return None
            shifts = reset_watcher.detect_shifts(before, snap.readings)
            with self.sm.begin() as session:
                insert_snapshot(session, snap)
                mark_source(session, "probe", now)
            self.last_probe_at = now
            for s in shifts:
                log.info(
                    "reset.shift",
                    bucket=s.bucket,
                    expected=s.expected.isoformat(),
                    reported=s.reported.isoformat(),
                )
                self.bus.publish(
                    Event(
                        "reset.shifted",
                        {
                            "bucket": s.bucket,
                            "expected": s.expected.isoformat(),
                            "reported": s.reported.isoformat(),
                        },
                    )
                )
            self.bus.publish(Event("limits.updated", {"source": "probe"}))
            return snap

    def add_manual_reading(self, bucket: str, used_pct: float) -> LimitReading:
        """Manual calibration (source D). Never carries a reset time: those are only read from Claude."""
        reading = LimitReading(
            ts=self.clock.now(), bucket=bucket, used_pct=used_pct, resets_at=None, source=ReadingSource.MANUAL
        )
        with self.sm.begin() as session:
            insert_readings(session, [reading])
            mark_source(session, "manual", reading.ts)
        self.bus.publish(Event("limits.updated", {"source": "manual"}))
        return reading

    async def run_forever(self) -> None:
        """The collector part of the daemon's tick loop."""
        while True:
            try:
                await asyncio.to_thread(self.ingest_once)
                with self.sm() as session:
                    latest = latest_readings(session)
                due = reset_watcher.next_probe_at(
                    self.clock.now(),
                    self.last_probe_at,
                    latest,
                    timedelta(seconds=self.settings.probe_interval_seconds),
                )
                if due <= self.clock.now():
                    await self.probe_once()
                if self.cost_model_due():
                    await asyncio.to_thread(self.refit_cost_models)
            except Exception:  # the loop must survive anything; errors are logged with context
                log.exception("collector.tick_failed")
            await asyncio.sleep(self.settings.tick_seconds)

    # ------------------------------------------------------------------ cost model

    def cost_model_due(self) -> bool:
        with self.sm() as session:
            stored = latest_cost_model_fit_at(session)
        attempts = [t for t in (stored, self.last_cost_fit_at) if t is not None]
        last = max(attempts) if attempts else None
        return last is None or self.clock.now() - last >= COST_MODEL_REFIT_EVERY

    def refit_cost_models(self) -> list[CostModelFit]:
        """Fit and store the cost model for every bucket with recent readings (plan §5.3)."""
        now = self.clock.now()
        start = now - COST_MODEL_HISTORY
        fits: list[CostModelFit] = []
        self.last_cost_fit_at = now
        with self.sm.begin() as session:
            for bucket in buckets_with_readings(session, start):
                readings, events = cost_model_inputs(session, bucket, start, now)
                fit = fit_cost_model(bucket, build_windows(bucket, readings, events), self.clock)
                stored = save_cost_model(session, fit)
                log.info(
                    "cost_model.fitted",
                    bucket=bucket,
                    readings=len(readings),
                    events=len(events),
                    windows=fit.n_windows,
                    r2=fit.r2,
                    low_confidence=fit.low_confidence,
                    reasons=list(fit.reasons),
                    coefficients={f: c.pct_per_weighted_token for f, c in fit.coefficients.items()},
                    stored=stored,
                )
                fits.append(fit)
        if fits:
            self.bus.publish(Event("cost_model.fitted", {"buckets": [f.bucket for f in fits]}))
        return fits

    def cost_model(self, bucket: str) -> CostModelFit | None:
        with self.sm() as session:
            return latest_cost_model(session, bucket)

    # ------------------------------------------------------------------ predictor inputs (plan §6)

    def user_observations(self, bucket: str, history: timedelta = FORECAST_HISTORY) -> list[SlotObservation]:
        """Per-slot user usage of `bucket` over the last `history` (ADR 0011)."""
        now = self.clock.now()
        with self.sm() as session:
            readings, raw = observation_inputs(session, bucket, now - history, now)
            fit = latest_cost_model(session, bucket)
        events = [
            CostedEvent(ts, estimate_pct(fit, [(model, tokens)]) if fit else None, agent)
            for ts, model, tokens, agent in raw
        ]
        return user_slot_observations(bucket, readings, events, now)

    def forecast_model(self, bucket: str) -> SeasonalBaseline:
        """The seasonal baseline for `bucket`, refitted at most every FORECAST_REFIT_EVERY."""
        now = self.clock.now()
        cached = self._baselines.get(bucket)
        if cached is not None and now - cached.fitted_at < FORECAST_REFIT_EVERY:
            return cached
        obs = self.user_observations(bucket)
        model = fit_baseline(bucket, obs, self.clock, self.settings.tz())
        self._baselines[bucket] = model
        log.info(
            "forecast.fitted",
            bucket=bucket,
            observations=len(obs),
            history_days=round(model.history_days, 2),
            cold_start=model.cold_start,
        )
        return model

    def activity(self, recent_jump: timedelta = RECENT_JUMP) -> Activity:
        """Is the user working right now? (plan §6.3)"""
        now = self.clock.now()
        start = now - recent_jump - timedelta(hours=1)
        with self.sm() as session:
            last_manual = last_manual_event_at(session)
            readings, raw = observation_inputs(session, FIVE_HOUR, start, now)
            fit = latest_cost_model(session, FIVE_HOUR)
        rises: list[ReadingRise] = []
        ordered = sorted((r for r in readings if r.resets_at is not None), key=lambda r: r.ts)
        for a, b in itertools.pairwise(ordered):
            assert a.resets_at is not None
            assert b.resets_at is not None
            if abs(a.resets_at - b.resets_at) > timedelta(minutes=5):
                continue  # a reset in between: the change says nothing about usage
            inside = [(m, t) for ts, m, t, _ in raw if a.ts < ts <= b.ts]
            explained = estimate_pct(fit, inside) if fit else None
            rises.append(ReadingRise(a.ts, b.ts, b.used_pct - a.used_pct, explained))
        return user_activity(now, last_manual, rises, recent_jump=recent_jump)

    # ------------------------------------------------------------------ queries

    def latest(self) -> list[LimitReading]:
        with self.sm() as session:
            return latest_readings(session)

    def resets_fresh(self) -> bool:
        with self.sm() as session:
            return reset_watcher.reset_times_fresh(latest_reset_reading(session), self.clock.now())

    def source_health(self) -> list[dict[str, object]]:
        now = self.clock.now()
        stale = timedelta(seconds=self.settings.stale_after_seconds)
        with self.sm() as session:
            rows = list(session.scalars(select(SourceStatusRow)))
        return [
            {
                "name": r.name,
                "last_ok_at": r.last_ok_at,
                "last_error_at": r.last_error_at,
                "last_error": r.last_error,
                "ok": r.last_ok_at is not None
                and now - r.last_ok_at <= stale
                and (r.last_error_at is None or r.last_error_at <= r.last_ok_at),
            }
            for r in rows
        ]

    def weekly_window(self) -> tuple[datetime, datetime] | None:
        """(start, reset) of the current weekly window, from the latest reported weekly reset."""
        for r in self.latest():
            if r.bucket == SEVEN_DAY and r.resets_at is not None:
                reset = reset_watcher.project_reset(r.resets_at, r.bucket, self.clock.now())
                return reset - timedelta(days=7), reset
        return None

    def _weighted_events(self, session: Session, start: datetime, end: datetime) -> list[WeightedEvent]:
        stmt = select(
            UsageEventRow.ts,
            UsageEventRow.model,
            UsageEventRow.input_tok,
            UsageEventRow.output_tok,
            UsageEventRow.cache_create_tok,
            UsageEventRow.cache_read_tok,
            UsageEventRow.is_agent,
        ).where(UsageEventRow.ts >= start, UsageEventRow.ts < end)
        return [
            WeightedEvent(
                ts=ts,
                weight=weighted_tokens(model, TokenCounts(input=i, output=o, cache_create=cc, cache_read=cr)),
                is_agent=agent,
            )
            for ts, model, i, o, cc, cr, agent in session.execute(stmt)
        ]

    def _surface_history(
        self, session: Session, start: datetime
    ) -> tuple[list[SurfaceShare], list[SurfacePoint]]:
        rows = list(
            session.scalars(
                select(SurfaceReadingRow).where(SurfaceReadingRow.ts >= start).order_by(SurfaceReadingRow.ts)
            )
        )
        by_ts: dict[datetime, list[SurfaceReadingRow]] = {}
        for r in rows:
            by_ts.setdefault(r.ts, []).append(r)
        points = [
            SurfacePoint(ts, sum(r.pct_of_limit for r in group if r.key != "claude_code"))
            for ts, group in by_ts.items()
        ]
        latest: list[SurfaceShare] = []
        if by_ts:
            last = by_ts[max(by_ts)]
            latest = [
                SurfaceShare(key=r.key, display_name=r.display_name, pct_of_limit=r.pct_of_limit)
                for r in last
            ]
        return latest, points

    def week_split(self) -> Split | None:
        window = self.weekly_window()
        if window is None:
            return None
        start, _ = window
        with self.sm() as session:
            events = self._weighted_events(session, start, self.clock.now())
            latest, _ = self._surface_history(session, start)
        manual_wt = sum(e.weight for e in events if not e.is_agent)
        agent_wt = sum(e.weight for e in events if e.is_agent)
        return weekly_split(latest, manual_wt, agent_wt)

    def week_series(self, step: timedelta = timedelta(hours=1)) -> list[SeriesPoint]:
        window = self.weekly_window()
        if window is None:
            return []
        start, reset = window
        end = min(reset, self.clock.now() + step)
        with self.sm() as session:
            events = self._weighted_events(session, start, end)
            latest, points = self._surface_history(session, start)
        visible = sum(s.pct_of_limit for s in latest if s.key == "claude_code")
        # Invisible usage is cumulative from the window start, where it is 0 by definition.
        baseline = [SurfacePoint(start, 0.0)] if points else []
        return hourly_series(events, baseline + points, visible, start, end, step)
