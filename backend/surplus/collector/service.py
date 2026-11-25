"""Collector service: the I/O shell around the pure collector core.

Runs inside `surplusd`: ingests logs and the status-line spool every tick, runs the Reset Watcher's probe on
schedule, and answers the dashboard's queries.
"""

from __future__ import annotations

import asyncio
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
    ingest_logs,
    insert_readings,
    insert_snapshot,
    latest_readings,
    latest_reset_reading,
    mark_source,
)
from surplus.config import Paths, Settings
from surplus.db.models import SourceStatusRow, SurfaceReadingRow, UsageEventRow
from surplus.domain.events import Event, EventBus
from surplus.domain.models import (
    SEVEN_DAY,
    LimitReading,
    ReadingSource,
    SurfaceShare,
    TokenCounts,
    UsageSnapshot,
)

log = structlog.get_logger(__name__)

ProbeFn = Callable[[], Awaitable[UsageSnapshot]]


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
            except Exception:  # the loop must survive anything; errors are logged with context
                log.exception("collector.tick_failed")
            await asyncio.sleep(self.settings.tick_seconds)

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
