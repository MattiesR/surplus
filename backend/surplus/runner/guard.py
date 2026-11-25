"""Run guard rails for manual runs (plan §7.3) — pure. Phase 4 adds the planner's `allowed_now` on top."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from surplus.domain.models import FIVE_HOUR, LimitReading


def is_weekly(bucket: str) -> bool:
    return bucket.startswith("seven_day") or bucket.startswith("weekly")


@dataclass(frozen=True)
class Limits:
    max_five_hour_pct: float
    max_weekly_pct: float

    def cap(self, bucket: str) -> float | None:
        if bucket == FIVE_HOUR:
            return self.max_five_hour_pct
        if is_weekly(bucket):
            return self.max_weekly_pct
        return None


def over_limit(readings: Iterable[LimitReading], limits: Limits) -> str | None:
    """A reason to stop, or None. Any bucket at or above its cap stops the run."""
    for r in readings:
        cap = limits.cap(r.bucket)
        if cap is not None and r.used_pct >= cap:
            return f"{r.bucket} is at {r.used_pct:.0f}% (hard stop at {cap:.0f}%)"
        if r.status and r.status not in ("allowed", "allowed_warning"):
            return f"{r.bucket} status is {r.status}"
    return None


def budget_pct(readings: Iterable[LimitReading], limits: Limits) -> float | None:
    """Room left in the 5-hour window before the hard stop, in percentage points."""
    for r in readings:
        if r.bucket == FIVE_HOUR:
            return max(0.0, limits.max_five_hour_pct - r.used_pct)
    return None
