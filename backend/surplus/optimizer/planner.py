"""Window planner (plan §7.2) — pure.

Plans per subscription window, until the weekly reset:
1. Window boundaries come from the latest *reported* 5-hour reset, projected as `reset + k x 5 h`
   (`reset_watcher.project_reset`), never computed from scratch. Recomputing from the newest reading every
   tick is the re-sync: when Claude reports a reset that moved by more than 5 minutes, `anchor_drift` says by
   how much (to log and show), and the new plan already follows it.
2. Each window's capacity is what fits next to the user's P90 usage, the window reserve and the hard stop.
3. The weekly surplus is spread over the windows in proportion to capacity x a back-loading weight that grows
   towards the weekly reset (capacity unused at a reset is lost, and uncertainty shrinks as it approaches),
   filled greedily up to each window's capacity; what one window cannot take goes to the others.
4. `allowed_now` is the current window's allocation, in pp of the 5-hour window.

Property (tested with hypothesis): under P90 user usage no window exceeds 100 - reserve or the hard stop, and
the week never exceeds 100 - weekly reserve or the weekly hard stop.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from surplus.clock import ensure_utc
from surplus.collector.reset_watcher import SHIFT_TOLERANCE, project_reset
from surplus.domain.models import FIVE_HOUR
from surplus.optimizer.budget import (
    BudgetConfig,
    WeeklyBudget,
    weekly_budget,
    window_capacity,
)
from surplus.predictor.baseline import SlotForecast, slot_floor

FIVE_HOURS = timedelta(hours=5)
DEFAULT_BUDGET = BudgetConfig()


@dataclass(frozen=True)
class PlanInputs:
    now: datetime
    five_reset: datetime  # latest reported reset of the 5-hour bucket
    weekly_reset: datetime  # latest reported reset of the weekly bucket
    five_used: float  # pp5, newest reading
    weekly_used: float  # ppw, newest reading
    five_forecast: Sequence[SlotForecast]  # user usage per slot, pp5, from now until the weekly reset
    weekly_forecast: Sequence[SlotForecast]  # user usage per slot, ppw, from now until the weekly reset
    confidence: float
    user_active: bool
    weekly_per_five: float  # ppw per pp5
    config: BudgetConfig = DEFAULT_BUDGET


@dataclass(frozen=True)
class PlannedWindow:
    start: datetime
    end: datetime
    current: bool
    manual_p90: float  # pp5 predicted for the user in [start, end)
    capacity: float  # pp5 an agent could use here
    allocation: float  # pp5 planned for agents
    reason: str


@dataclass(frozen=True)
class Plan:
    generated_at: datetime
    anchor: datetime  # the 5-hour reset the windows are projected from
    weekly_reset: datetime
    weekly: WeeklyBudget
    windows: tuple[PlannedWindow, ...]
    weekly_per_five: float

    @property
    def allowed_now(self) -> float:
        return self.windows[0].allocation if self.windows else 0.0

    @property
    def planned_weekly(self) -> float:
        return sum(w.allocation for w in self.windows) * self.weekly_per_five


def _sum(forecast: Sequence[SlotForecast], start: datetime, end: datetime) -> float:
    lo = slot_floor(start)
    return sum(f.p90_pct for f in forecast if lo <= ensure_utc(f.slot_start) < end)


def window_bounds(
    now: datetime, five_reset: datetime, weekly_reset: datetime
) -> list[tuple[datetime, datetime]]:
    """The current 5-hour window (from now) and the projected ones that start before the weekly reset."""
    now, weekly_reset = ensure_utc(now), ensure_utc(weekly_reset)
    end = project_reset(ensure_utc(five_reset), FIVE_HOUR, now)
    out = [(now, min(end, weekly_reset))] if now < weekly_reset else []
    while end < weekly_reset:
        out.append((end, min(end + FIVE_HOURS, weekly_reset)))
        end += FIVE_HOURS
    return out


def _distribute(capacity: list[float], weights: list[float], total: float) -> list[float]:
    """Water-filling: shares of `total` by weight, capped at capacity; the excess goes to the others."""
    alloc = [0.0] * len(capacity)
    open_ = {i for i, c in enumerate(capacity) if c > 0 and weights[i] > 0}
    left = total
    while left > 1e-9 and open_:
        wsum = sum(weights[i] for i in open_)
        spilled = 0.0
        for i in sorted(open_):
            want = left * weights[i] / wsum
            room = capacity[i] - alloc[i]
            take = min(want, room)
            alloc[i] += take
            spilled += want - take
        open_ = {i for i in open_ if capacity[i] - alloc[i] > 1e-9}
        left = spilled
    return alloc


def plan(inputs: PlanInputs) -> Plan:
    cfg = inputs.config
    now = ensure_utc(inputs.now)
    weekly_reset = project_reset(ensure_utc(inputs.weekly_reset), "seven_day", now)
    bounds = window_bounds(now, inputs.five_reset, weekly_reset)
    weekly = weekly_budget(
        inputs.weekly_used,
        _sum(inputs.weekly_forecast, now, weekly_reset),
        inputs.confidence,
        weekly_reset - now,
        cfg,
    )
    manual = [_sum(inputs.five_forecast, s, e) for s, e in bounds]
    capacity = [
        window_capacity(inputs.five_used if i == 0 else 0.0, m, inputs.user_active and i == 0, cfg)
        for i, m in enumerate(manual)
    ]
    n = len(bounds)
    weights = [c * (1 + cfg.backload * (i / (n - 1) if n > 1 else 1.0)) for i, c in enumerate(capacity)]
    r = max(inputs.weekly_per_five, 1e-9)
    alloc = _distribute(capacity, weights, weekly.surplus / r)
    windows = []
    for i, (s, e) in enumerate(bounds):
        if i == 0 and inputs.user_active:
            reason = "you are active: no agent work in this window"
        elif capacity[i] <= 0:
            reason = f"predicted busy: {manual[i]:.1f} pp for you (P90) + reserve leave no room"
        elif alloc[i] <= 0:
            reason = "no weekly surplus left for this window"
        else:
            reason = f"{alloc[i]:.1f} of {capacity[i]:.1f} pp: weekly surplus, weighted towards the reset"
        windows.append(PlannedWindow(s, e, i == 0, manual[i], capacity[i], alloc[i], reason))
    return Plan(
        now, ensure_utc(inputs.five_reset), weekly_reset, weekly, tuple(windows), inputs.weekly_per_five
    )


def anchor_drift(previous: Plan | None, reported_reset: datetime) -> timedelta | None:
    """How far a newly reported 5-hour reset moved from the previous plan's grid, if more than 5 minutes."""
    if previous is None:
        return None
    reported = ensure_utc(reported_reset)
    k = round((reported - previous.anchor) / FIVE_HOURS)  # nearest boundary of the previous grid
    delta = reported - (previous.anchor + k * FIVE_HOURS)
    return delta if abs(delta) > SHIFT_TOLERANCE else None
