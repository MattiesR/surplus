"""Time abstraction. Every time-dependent component takes a `Clock`, so tests can simulate weeks in ms."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as an aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock needs an aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta

    def set(self, when: datetime) -> None:
        self._now = when.astimezone(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC (SQLite drops tzinfo) and convert aware ones to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
