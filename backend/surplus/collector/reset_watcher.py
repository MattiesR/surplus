"""Reset Watcher — pure scheduling logic (plan §2.3, §5.1).

Reset times are only ever *read* from Claude. This module decides when to read them again, rolls a reported
reset forward to project the next one, notices when Claude reports something different from what was
expected, and says when the schedule is running on guesses (no fresh reset time for > 1 h).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from surplus.domain.models import LimitReading, bucket_period_hours

PROBE_INTERVAL = timedelta(minutes=15)
AFTER_RESET_DELAY = timedelta(minutes=3)
SHIFT_TOLERANCE = timedelta(minutes=5)
MAX_RESET_AGE = timedelta(hours=1)


def project_reset(resets_at: datetime, bucket: str, now: datetime) -> datetime:
    """The next reset at or after `now`, projected as `last reported reset + k x period`."""
    period = timedelta(hours=bucket_period_hours(bucket))
    if resets_at > now:
        return resets_at
    k = (now - resets_at) // period + 1
    return resets_at + k * period


def next_probe_at(
    now: datetime,
    last_probe_at: datetime | None,
    latest: Iterable[LimitReading],
    interval: timedelta = PROBE_INTERVAL,
) -> datetime:
    """Poll every `interval`, and additionally a few minutes after each expected reset."""
    if last_probe_at is None:
        return now
    candidates = [last_probe_at + interval]
    for r in latest:
        if r.resets_at is None:
            continue
        # A reset that has not been confirmed by a probe after it → probe a few minutes after it.
        confirm_at = r.resets_at + AFTER_RESET_DELAY
        if confirm_at > last_probe_at:
            candidates.append(confirm_at)
    return max(now, min(candidates))


@dataclass(frozen=True)
class ResetShift:
    bucket: str
    expected: datetime
    reported: datetime

    @property
    def delta(self) -> timedelta:
        return self.reported - self.expected


def detect_shifts(
    expected: Mapping[str, datetime],
    new_readings: Iterable[LimitReading],
    tolerance: timedelta = SHIFT_TOLERANCE,
) -> list[ResetShift]:
    """Readings whose reported reset differs from the projected one by more than `tolerance`."""
    shifts: list[ResetShift] = []
    for r in new_readings:
        if r.resets_at is None or r.bucket not in expected:
            continue
        exp = project_reset(expected[r.bucket], r.bucket, r.ts)
        if abs(r.resets_at - exp) > tolerance:
            shifts.append(ResetShift(r.bucket, exp, r.resets_at))
    return shifts


def expectations(latest: Iterable[LimitReading]) -> dict[str, datetime]:
    return {r.bucket: r.resets_at for r in latest if r.resets_at is not None}


def reset_times_fresh(last_reset_reading_at: datetime | None, now: datetime) -> bool:
    """False means agent runs must pause until a fresh reading arrives (plan §5.1)."""
    return last_reset_reading_at is not None and now - last_reset_reading_at <= MAX_RESET_AGE
