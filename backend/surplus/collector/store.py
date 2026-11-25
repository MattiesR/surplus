"""Persistence for the collector: idempotent inserts, ingest cursors, source health, latest readings."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, Result, func, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import Clock
from surplus.collector.jsonl import FileCursor, UsageTag, iter_log_files, read_new_lines
from surplus.db.models import (
    CostModelRow,
    IngestCursorRow,
    LimitReadingRow,
    SourceStatusRow,
    SurfaceReadingRow,
    UsageEventRow,
)
from surplus.domain.models import LimitReading, ReadingSource, TokenCounts, UsageEvent, UsageSnapshot
from surplus.predictor.cost_model import (
    CoefficientSource,
    CostModelFit,
    ModelCoefficient,
    TokenEvent,
    TokenWeights,
)

log = structlog.get_logger(__name__)

# SQLite allows at most 32766 bound parameters per statement; 15 columns per row → chunk well below.
_CHUNK = 1000


def _rowcount(result: Result[Any]) -> int:
    return int(cast(CursorResult[Any], result).rowcount or 0)


def insert_events(session: Session, events: Iterable[UsageEvent]) -> int:
    rows = [
        {
            "message_id": e.message_id,
            "ts": e.ts,
            "source": e.source.value,
            "session_id": e.session_id,
            "project_id": e.project_id,
            "run_id": e.run_id,
            "model": e.model,
            "input_tok": e.tokens.input,
            "output_tok": e.tokens.output,
            "cache_create_tok": e.tokens.cache_create,
            "cache_read_tok": e.tokens.cache_read,
            "is_agent": e.is_agent,
            "is_sidechain": e.is_sidechain,
            "entrypoint": e.entrypoint,
        }
        for e in events
    ]
    inserted = 0
    for i in range(0, len(rows), _CHUNK):
        stmt = insert(UsageEventRow).values(rows[i : i + _CHUNK]).on_conflict_do_nothing()
        inserted += _rowcount(session.execute(stmt))
    return inserted


def insert_readings(session: Session, readings: Iterable[LimitReading]) -> int:
    rows = [
        {
            "ts": r.ts,
            "bucket": r.bucket,
            "used_pct": r.used_pct,
            "resets_at": r.resets_at,
            "source": r.source.value,
            "status": r.status,
        }
        for r in readings
    ]
    if not rows:
        return 0
    stmt = insert(LimitReadingRow).values(rows).on_conflict_do_nothing()
    return _rowcount(session.execute(stmt))


def insert_snapshot(session: Session, snap: UsageSnapshot) -> int:
    n = insert_readings(session, snap.readings)
    if snap.weekly_breakdown:
        stmt = (
            insert(SurfaceReadingRow)
            .values(
                [
                    {
                        "ts": snap.ts,
                        "key": s.key,
                        "display_name": s.display_name,
                        "pct_of_limit": s.pct_of_limit,
                        "window_started_at": snap.weekly_window_started_at,
                    }
                    for s in snap.weekly_breakdown
                ]
            )
            .on_conflict_do_nothing()
        )
        session.execute(stmt)
    return n


def mark_source(session: Session, name: str, now: datetime, error: str | None = None) -> None:
    row = session.get(SourceStatusRow, name)
    if row is None:
        row = SourceStatusRow(name=name)
        session.add(row)
    if error is None:
        row.last_ok_at = now
    else:
        row.last_error_at = now
        row.last_error = error[:2000]


def get_cursor(session: Session, path: str) -> FileCursor | None:
    row = session.get(IngestCursorRow, path)
    return None if row is None else FileCursor(row.path, row.inode, row.offset)


def save_cursor(session: Session, cursor: FileCursor, now: datetime) -> None:
    row = session.get(IngestCursorRow, cursor.path)
    if row is None:
        session.add(
            IngestCursorRow(path=cursor.path, inode=cursor.inode, offset=cursor.offset, updated_at=now)
        )
    else:
        row.inode, row.offset, row.updated_at = cursor.inode, cursor.offset, now


@dataclass
class IngestStats:
    files: int = 0
    files_changed: int = 0
    lines: int = 0
    bad_lines: int = 0
    events_seen: int = 0
    events_inserted: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


def ingest_logs(
    sm: sessionmaker[Session],
    roots: tuple[Path, ...],
    clock: Clock,
    tag: UsageTag | None = None,
    source_name: str = "cc_log",
) -> IngestStats:
    """Incrementally ingest every JSONL log under `roots`. Safe to run repeatedly (exactly-once)."""
    t0 = time.perf_counter()
    stats = IngestStats()
    with sm() as session:
        cursors = {row.path: row for row in session.scalars(select(IngestCursorRow))}
    for path in iter_log_files(roots):
        stats.files += 1
        key = str(path)
        try:
            st = path.stat()
            known = cursors.get(key)
            if known is not None and known.inode == st.st_ino and known.offset == st.st_size:
                continue  # unchanged since the last pass
            cursor = None if known is None else FileCursor(known.path, known.inode, known.offset)
            result = read_new_lines(path, cursor, tag)
        except OSError as exc:
            stats.errors.append(f"{path}: {exc}")
            continue
        stats.files_changed += 1
        stats.lines += result.lines_read
        stats.bad_lines += result.bad_lines
        stats.events_seen += len(result.events)
        with sm.begin() as session:
            stats.events_inserted += insert_events(session, result.events)
            save_cursor(session, result.cursor, clock.now())
    with sm.begin() as session:
        mark_source(session, source_name, clock.now(), "; ".join(stats.errors) or None)
    stats.seconds = time.perf_counter() - t0
    log.info(
        "ingest.logs",
        files=stats.files,
        changed=stats.files_changed,
        inserted=stats.events_inserted,
        seconds=round(stats.seconds, 3),
    )
    return stats


def latest_readings(session: Session) -> list[LimitReading]:
    """The most recent reading per bucket, across all sources."""
    sub = (
        select(LimitReadingRow.bucket, func.max(LimitReadingRow.ts).label("max_ts"))
        .group_by(LimitReadingRow.bucket)
        .subquery()
    )
    stmt = (
        select(LimitReadingRow)
        .join(sub, (LimitReadingRow.bucket == sub.c.bucket) & (LimitReadingRow.ts == sub.c.max_ts))
        .order_by(LimitReadingRow.bucket, LimitReadingRow.id.desc())
    )
    out: dict[str, LimitReading] = {}
    for row in session.scalars(stmt):
        if row.bucket not in out:
            out[row.bucket] = row_to_reading(row)
    return list(out.values())


def latest_reset_reading(session: Session) -> datetime | None:
    """When a reset time was last reported by any source (manual readings never carry one)."""
    return session.scalar(select(func.max(LimitReadingRow.ts)).where(LimitReadingRow.resets_at.is_not(None)))


def row_to_reading(row: LimitReadingRow) -> LimitReading:
    return LimitReading(
        ts=row.ts,
        bucket=row.bucket,
        used_pct=row.used_pct,
        resets_at=row.resets_at,
        source=ReadingSource(row.source),
        status=row.status,
    )


# ------------------------------------------------------------------------------- cost model (plan §5.3)


def cost_model_inputs(
    session: Session, bucket: str, start: datetime, end: datetime
) -> tuple[list[LimitReading], list[TokenEvent]]:
    """Readings of `bucket` and token events in `[start, end]`, the raw material for `build_windows`."""
    readings = [
        row_to_reading(r)
        for r in session.scalars(
            select(LimitReadingRow)
            .where(LimitReadingRow.bucket == bucket, LimitReadingRow.ts >= start, LimitReadingRow.ts <= end)
            .order_by(LimitReadingRow.ts)
        )
    ]
    stmt = select(
        UsageEventRow.ts,
        UsageEventRow.model,
        UsageEventRow.input_tok,
        UsageEventRow.output_tok,
        UsageEventRow.cache_create_tok,
        UsageEventRow.cache_read_tok,
    ).where(UsageEventRow.ts >= start, UsageEventRow.ts <= end)
    events = [
        TokenEvent(ts, model, TokenCounts(input=i, output=o, cache_create=cc, cache_read=cr))
        for ts, model, i, o, cc, cr in session.execute(stmt)
    ]
    return readings, events


def save_cost_model(session: Session, fit: CostModelFit) -> int:
    """Store one fit as one row per model family (with its R²). A fit without coefficients stores nothing."""
    weights = json.dumps(fit.weights.as_dict(), sort_keys=True)
    reasons = json.dumps(list(fit.reasons))
    for c in fit.coefficients.values():
        session.add(
            CostModelRow(
                bucket=fit.bucket,
                model=c.family,
                ts_fitted=fit.fitted_at,
                pct_per_weighted_token=c.pct_per_weighted_token,
                weights_json=weights,
                r2=fit.r2,
                source=c.source.value,
                n_windows=fit.n_windows,
                model_windows=c.n_windows,
                low_confidence=fit.low_confidence,
                reasons_json=reasons,
            )
        )
    return len(fit.coefficients)


def latest_cost_model(session: Session, bucket: str) -> CostModelFit | None:
    """The most recent stored fit for `bucket`."""
    ts = session.scalar(select(func.max(CostModelRow.ts_fitted)).where(CostModelRow.bucket == bucket))
    if ts is None:
        return None
    rows = list(
        session.scalars(
            select(CostModelRow).where(CostModelRow.bucket == bucket, CostModelRow.ts_fitted == ts)
        )
    )
    first = rows[0]
    return CostModelFit(
        bucket=bucket,
        fitted_at=first.ts_fitted,
        weights=TokenWeights(**json.loads(first.weights_json)),
        coefficients={
            r.model: ModelCoefficient(
                r.model, r.pct_per_weighted_token, CoefficientSource(r.source), r.model_windows
            )
            for r in rows
        },
        r2=first.r2,
        n_windows=first.n_windows,
        low_confidence=first.low_confidence,
        reasons=tuple(json.loads(first.reasons_json)),
    )


def latest_cost_model_fit_at(session: Session) -> datetime | None:
    return session.scalar(select(func.max(CostModelRow.ts_fitted)))


def buckets_with_readings(session: Session, since: datetime) -> list[str]:
    """Buckets with at least one reading since `since`: the ones worth fitting."""
    return list(
        session.scalars(
            select(LimitReadingRow.bucket).where(LimitReadingRow.ts >= since).distinct().order_by("bucket")
        )
    )
