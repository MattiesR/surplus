"""Budget (plan §7.1) and window planner (§7.2): unit cases plus the hypothesis safety property."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from surplus.optimizer.budget import (
    BudgetConfig,
    sweep_factor,
    weekly_budget,
    window_capacity,
    window_reserve,
)
from surplus.optimizer.planner import PlanInputs, anchor_drift, plan, window_bounds
from surplus.predictor.baseline import SLOT, SlotBasis, SlotForecast, slot_floor

NOW = datetime(2026, 11, 26, 9, 7, tzinfo=UTC)
EPS = 1e-6


def flat(bucket: str, start: datetime, end: datetime, per_slot: float) -> list[SlotForecast]:
    out, cur = [], slot_floor(start)
    while cur < end:
        out.append(SlotForecast(cur, bucket, per_slot, per_slot, SlotBasis.HISTORY))
        cur += SLOT
    return out


def inputs(**kw: object) -> PlanInputs:
    weekly_reset = kw.pop("weekly_reset", NOW + timedelta(days=3))
    assert isinstance(weekly_reset, datetime)
    base: dict[str, object] = {
        "now": NOW,
        "five_reset": NOW + timedelta(hours=2),
        "weekly_reset": weekly_reset,
        "five_used": 10.0,
        "weekly_used": 20.0,
        "five_forecast": flat("five_hour", NOW, weekly_reset, 0.0),
        "weekly_forecast": flat("seven_day", NOW, weekly_reset, 0.0),
        "confidence": 1.0,
        "user_active": False,
        "weekly_per_five": 0.2,
    }
    base.update(kw)
    return PlanInputs(**base)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------------- budget


def test_weekly_budget_formula_and_confidence() -> None:
    cfg = BudgetConfig()
    b = weekly_budget(20, 30, 1.0, timedelta(days=3), cfg)
    assert (b.free, b.reserve, b.surplus) == (80, 10, 40)
    assert weekly_budget(20, 30, 0.0, timedelta(days=3), cfg).reserve == 20  # low confidence doubles it
    capped = weekly_budget(20, 30, 1.0, timedelta(days=3), BudgetConfig(max_weekly_pct=60))
    assert capped.surplus == 10 and any("hard stop" in r for r in capped.reasons)


def test_sweep_shrinks_the_reserve_step_by_step() -> None:
    cfg = BudgetConfig()
    assert sweep_factor(timedelta(hours=50), cfg) == 1
    assert sweep_factor(timedelta(hours=24), cfg) == pytest.approx(0.5)
    assert sweep_factor(timedelta(hours=23, minutes=10), cfg) == pytest.approx(0.5)  # hourly steps
    assert sweep_factor(timedelta(minutes=5), cfg) == cfg.sweep_floor


def test_window_capacity() -> None:
    cfg = BudgetConfig()
    assert window_capacity(10, 20, False, cfg) == 45  # 100 - 10 - 20 - 25
    assert window_capacity(10, 20, True, cfg) == 0  # user active: everything reserved
    assert window_reserve(True, cfg) == 100
    assert window_capacity(10, 0, False, BudgetConfig(base_window_reserve=0)) == 75  # the 85% hard stop
    assert window_capacity(80, 30, False, cfg) == 0


# ----------------------------------------------------------------------------------------- planner


def test_windows_are_projected_from_the_reported_reset() -> None:
    b = window_bounds(NOW, NOW - timedelta(hours=3), NOW + timedelta(hours=12))
    # the reported reset lies in the past: next boundary = reset + 5 h = NOW + 2 h
    assert [e - s for s, e in b] == [timedelta(hours=2), timedelta(hours=5), timedelta(hours=5)]
    assert b[-1][1] == NOW + timedelta(hours=12)


def test_surplus_is_back_loaded_and_allowed_now_is_the_current_share() -> None:
    p = plan(inputs())
    allocs = [w.allocation for w in p.windows]
    assert allocs == sorted(allocs)  # equal capacity: later windows get more
    assert p.allowed_now == allocs[0] > 0
    assert p.planned_weekly == pytest.approx(p.weekly.surplus)


def test_busy_windows_get_nothing_and_their_share_spills_over() -> None:
    reset = NOW + timedelta(hours=12)
    five = flat("five_hour", NOW, reset, 0.0)
    busy = [
        SlotForecast(f.slot_start, f.bucket, 10, 10, f.basis)
        if NOW + timedelta(hours=2) <= f.slot_start < NOW + timedelta(hours=7)
        else f
        for f in five
    ]
    p = plan(inputs(weekly_reset=reset, five_forecast=busy))
    assert p.windows[1].capacity == 0 and p.windows[1].allocation == 0
    assert "predicted busy" in p.windows[1].reason


def test_user_active_blocks_the_current_window_only() -> None:
    p = plan(inputs(user_active=True))
    assert p.allowed_now == 0 and "active" in p.windows[0].reason
    assert sum(w.allocation for w in p.windows[1:]) > 0


def test_anchor_drift() -> None:
    p = plan(inputs())
    assert anchor_drift(p, p.anchor + timedelta(hours=5, minutes=3)) is None
    assert anchor_drift(p, p.anchor + timedelta(hours=10, minutes=20)) == timedelta(minutes=20)
    assert anchor_drift(None, p.anchor) is None
    moved = plan(inputs(five_reset=NOW + timedelta(hours=2, minutes=20)))
    assert moved.windows[0].end == NOW + timedelta(hours=2, minutes=20)  # the plan follows the new reset


# ----------------------------------------------------------------------------------------- property

pct = st.floats(min_value=0, max_value=100, allow_nan=False)
small = st.floats(min_value=0, max_value=3, allow_nan=False)


@settings(max_examples=150, deadline=None)
@given(
    five_used=pct,
    weekly_used=pct,
    five_profile=st.lists(small, min_size=24, max_size=24),
    weekly_profile=st.lists(st.floats(0, 0.5), min_size=24, max_size=24),
    reset_in=st.integers(1, 300),
    weekly_hours=st.integers(1, 7 * 24),
    confidence=st.floats(0, 1),
    active=st.booleans(),
    r=st.floats(0.02, 0.6),
    cap5=st.floats(50, 95),
    capw=st.floats(50, 95),
    reserve5=st.floats(0, 50),
    reservew=st.floats(0, 30),
)
def test_no_plan_exceeds_a_limit_under_p90_usage(
    five_used: float,
    weekly_used: float,
    five_profile: list[float],
    weekly_profile: list[float],
    reset_in: int,
    weekly_hours: int,
    confidence: float,
    active: bool,
    r: float,
    cap5: float,
    capw: float,
    reserve5: float,
    reservew: float,
) -> None:
    weekly_reset = NOW + timedelta(hours=weekly_hours)

    def expand(bucket: str, profile: list[float]) -> list[SlotForecast]:
        out = []
        for f in flat(bucket, NOW, weekly_reset, 0.0):
            v = profile[f.slot_start.hour]
            out.append(SlotForecast(f.slot_start, bucket, v, v, f.basis))
        return out

    cfg = BudgetConfig(
        base_window_reserve=reserve5,
        base_weekly_reserve=reservew,
        max_five_hour_pct=cap5,
        max_weekly_pct=capw,
    )
    p = plan(
        PlanInputs(
            now=NOW,
            five_reset=NOW + timedelta(minutes=reset_in),
            weekly_reset=weekly_reset,
            five_used=five_used,
            weekly_used=weekly_used,
            five_forecast=expand("five_hour", five_profile),
            weekly_forecast=expand("seven_day", weekly_profile),
            confidence=confidence,
            user_active=active,
            weekly_per_five=r,
            config=cfg,
        )
    )
    for i, w in enumerate(p.windows):
        used = five_used if i == 0 else 0.0
        assert w.allocation >= 0
        assert w.allocation <= w.capacity + EPS
        if w.allocation > 0:
            reserve = 100.0 if (active and i == 0) else reserve5
            assert used + w.manual_p90 + w.allocation <= 100 - reserve + EPS
            assert used + w.manual_p90 + w.allocation <= cap5 + EPS
    if active:
        assert p.allowed_now == 0
    total_weekly = p.planned_weekly
    assert total_weekly <= p.weekly.surplus + EPS
    if total_weekly > 0:
        mf = p.weekly.manual_future
        assert weekly_used + mf + total_weekly <= 100 - p.weekly.reserve + EPS
        assert weekly_used + mf + total_weekly <= capw + EPS
