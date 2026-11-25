"""Status-line spooler (opt-in source C, ADR 0002).

`surplus statusline` is configured as Claude Code's status-line command. It reads the status JSON from stdin,
appends only the `rate_limits` part (no cwd, no transcript path) to a spool file, and prints a short status.
It must be fast and must never fail, because it runs on every status-line render of the user's session.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import Clock
from surplus.collector.jsonl import FileCursor
from surplus.collector.limits import parse_statusline
from surplus.collector.store import get_cursor, insert_readings, mark_source, save_cursor
from surplus.domain.models import LimitReading

MAX_SPOOL_BYTES = 20 * 1024 * 1024


def spool_line(raw_stdin: str, now: datetime) -> tuple[str | None, str]:
    """Return (line to append or None, text to print)."""
    try:
        payload = json.loads(raw_stdin)
    except json.JSONDecodeError:
        return None, ""
    if not isinstance(payload, dict):
        return None, ""
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None, ""
    line = json.dumps({"ts": now.isoformat(), "rate_limits": rate_limits}, separators=(",", ":"))
    return line, render_status(rate_limits)


def render_status(rate_limits: Mapping[str, Any]) -> str:
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        entry = rate_limits.get(key)
        if isinstance(entry, Mapping) and isinstance(entry.get("used_percentage"), int | float):
            parts.append(f"{label} {entry['used_percentage']:.0f}%")
    return " · ".join(parts)


def append_to_spool(spool: Path, line: str) -> None:
    spool.parent.mkdir(parents=True, exist_ok=True)
    if spool.exists() and spool.stat().st_size > MAX_SPOOL_BYTES:
        spool.replace(spool.with_suffix(".old"))
    # O_APPEND writes of a single short line are atomic enough for concurrent renders.
    fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


def parse_spool_line(raw: bytes) -> list[LimitReading]:
    try:
        rec = json.loads(raw)
        ts = datetime.fromisoformat(rec["ts"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return []
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return parse_statusline(rec, ts)


def ingest_spool(sm: sessionmaker[Session], spool: Path, clock: Clock) -> int:
    if not spool.exists():
        return 0
    with sm() as session:
        cursor = get_cursor(session, str(spool))
    st = spool.stat()
    offset = cursor.offset if cursor and cursor.inode == st.st_ino and cursor.offset <= st.st_size else 0
    readings: list[LimitReading] = []
    with spool.open("rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            readings.extend(parse_spool_line(raw))
    with sm.begin() as session:
        n = insert_readings(session, readings)
        save_cursor(session, FileCursor(str(spool), st.st_ino, offset), clock.now())
        if readings:
            mark_source(session, "statusline", clock.now())
    return n
