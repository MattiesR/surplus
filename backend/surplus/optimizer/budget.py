"""Budget computation (plan §7.1) — pure. Units: percent of a bucket's own limit (0-100).

Two buckets, two units: the 5-hour window (`pp5`) and the weekly limit (`ppw`). `weekly_per_five` converts:
spending 1 pp of the 5-hour window costs `weekly_per_five` pp of the weekly limit. It comes from the cost
models of both buckets when they are trustworthy, else from a conservative prior (ADR 0012).

    weekly_free    = 100 - weekly_used
    manual_future  = sum of P90 user usage (weekly) until the weekly reset
    reserve_weekly = base_reserve * (2 - confidence) * sweep(hours to the weekly reset)
    weekly_surplus = max(0, min(weekly_free - manual_future - reserve_weekly,
                                weekly_cap - weekly_used - manual_future))

    window_capacity = max(0, min(100 - used - manual_p90 - reserve_window,
                                 five_hour_cap - used - manual_p90))
    reserve_window  = 100 if the user is active (current window only) else base_window_reserve
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta

BASE_WEEKLY_RESERVE = 10.0  # ppw
BASE_WINDOW_RESERVE = 25.0  # pp5
WEEKLY_PER_FIVE_PRIOR = 0.2  # a full 5-h window = 20% of the week: assumes a small plan (conservative)
SWEEP = timedelta(hours=48)  # the end-of-week sweep (plan §7.2.3)
SWEEP_FLOOR = 0.25  # the weekly reserve never shrinks below a quarter of itself
BACKLOAD = 1.0  # the window just before the weekly reset weighs (1 + BACKLOAD) x the current one


@dataclass(frozen=True)
class BudgetConfig:
    base_weekly_reserve: float = BASE_WEEKLY_RESERVE
    base_window_reserve: float = BASE_WINDOW_RESERVE
    max_five_hour_pct: float = 85.0
    max_weekly_pct: float = 90.0
    sweep: timedelta = SWEEP
    sweep_floor: float = SWEEP_FLOOR
    backload: float = BACKLOAD

    def __post_init__(self) -> None:
        for name in ("base_weekly_reserve", "base_window_reserve", "max_five_hour_pct", "max_weekly_pct"):
            v = getattr(self, name)
            if not (0 <= v <= 100):
                raise ValueError(f"{name} must be within 0..100, got {v}")
        if not (0 < self.sweep_floor <= 1):
            raise ValueError("sweep_floor must be within (0, 1]")


@dataclass(frozen=True)
class WeeklyBudget:
    free: float
    manual_future: float
    reserve: float
    surplus: float
    reasons: tuple[str, ...] = field(default=())


def _finite(*values: float) -> None:
    for v in values:
        if not math.isfinite(v):
            raise ValueError(f"non-finite budget input: {v}")


def sweep_factor(to_reset: timedelta, cfg: BudgetConfig) -> float:
    """1 outside the sweep; then shrinks in hourly steps towards `sweep_floor` as the reset approaches."""
    if to_reset >= cfg.sweep:
        return 1.0
    hours_left = math.ceil(max(to_reset, timedelta(0)) / timedelta(hours=1))
    total = cfg.sweep / timedelta(hours=1)
    return max(cfg.sweep_floor, hours_left / total)


def weekly_reserve(confidence: float, to_reset: timedelta, cfg: BudgetConfig) -> float:
    c = min(1.0, max(0.0, confidence))
    return cfg.base_weekly_reserve * (2 - c) * sweep_factor(to_reset, cfg)


def weekly_budget(
    weekly_used: float, manual_future: float, confidence: float, to_reset: timedelta, cfg: BudgetConfig
) -> WeeklyBudget:
    _finite(weekly_used, manual_future, confidence)
    free = max(0.0, 100.0 - weekly_used)
    reserve = weekly_reserve(confidence, to_reset, cfg)
    by_reserve = free - max(0.0, manual_future) - reserve
    by_cap = cfg.max_weekly_pct - weekly_used - max(0.0, manual_future)
    surplus = max(0.0, min(by_reserve, by_cap))
    reasons = [
        f"weekly: {free:.1f} free - {manual_future:.1f} predicted for you (P90) - {reserve:.1f} reserve"
        f" = {max(0.0, by_reserve):.1f}"
    ]
    if by_cap < by_reserve:
        reasons.append(f"weekly hard stop {cfg.max_weekly_pct:.0f}% leaves {max(0.0, by_cap):.1f}")
    if sweep_factor(to_reset, cfg) < 1:
        reasons.append(f"end-of-week sweep: reserve x{sweep_factor(to_reset, cfg):.2f}")
    return WeeklyBudget(free, manual_future, reserve, surplus, tuple(reasons))


def window_reserve(user_active: bool, cfg: BudgetConfig) -> float:
    return 100.0 if user_active else cfg.base_window_reserve


def window_capacity(used: float, manual_p90: float, user_active: bool, cfg: BudgetConfig) -> float:
    """Agent pp5 that fit in a window next to the user's P90 usage, its reserve and the hard stop."""
    _finite(used, manual_p90)
    manual = max(0.0, manual_p90)
    by_reserve = 100.0 - used - manual - window_reserve(user_active, cfg)
    by_cap = cfg.max_five_hour_pct - used - manual
    return max(0.0, min(by_reserve, by_cap))
