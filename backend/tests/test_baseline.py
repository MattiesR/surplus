"""Seasonal quantile baseline (plan §6.1): decay, neighbour smoothing, weighted quantiles, cold-start prior."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from surplus.clock import FakeClock
from surplus.domain.models import FIVE_HOUR, SEVEN_DAY
from surplus.predictor.baseline import (
    SLOT,
    SLOTS_PER_DAY,
    SLOTS_PER_WEEK,
    BaselineConfig,
    SlotBasis,
    SlotObservation,
    decay_weight,
    dense_observations,
    fit_baseline,
    hour_of_week,
    no_data_pct,
    slot_floor,
    weighted_quantile,
)

# A Monday 00:00 UTC; "now" is a few weeks later, also on a Monday 00:00.
MONDAY = datetime(2026, 10, 5, tzinfo=UTC)
WEEK = timedelta(weeks=1)


def _weeks(n: int, value: Callable[[int, int], float], start: datetime = MONDAY) -> list[SlotObservation]:
    """Dense observations for `n` weeks; `value(week, hour_of_week)` gives each slot's manual %."""
    return [
        SlotObservation(start + w * WEEK + i * SLOT, value(w, i))
        for w in range(n)
        for i in range(SLOTS_PER_WEEK)
    ]


def _clock_after(weeks: int, start: datetime = MONDAY) -> FakeClock:
    return FakeClock(start + weeks * WEEK)


# --------------------------------------------------------------------------------------------- helpers


def test_slot_floor_and_hour_of_week() -> None:
    assert slot_floor(datetime(2026, 10, 5, 9, 14, 59, 999, tzinfo=UTC)) == datetime(
        2026, 10, 5, 9, 0, tzinfo=UTC
    )
    assert slot_floor(datetime(2026, 10, 5, 9, 15, tzinfo=UTC)) == datetime(2026, 10, 5, 9, 15, tzinfo=UTC)
    assert slot_floor(datetime(2026, 10, 5, 9, 7)) == datetime(2026, 10, 5, 9, 0, tzinfo=UTC)  # naive = UTC
    assert hour_of_week(MONDAY) == 0
    assert hour_of_week(MONDAY + timedelta(days=6, hours=23, minutes=45)) == SLOTS_PER_WEEK - 1
    assert hour_of_week(MONDAY + timedelta(hours=9, minutes=30)) == 9 * 4 + 2


def test_hour_of_week_uses_local_time() -> None:
    cet = timezone(timedelta(hours=1))
    # Sunday 23:00 UTC is Monday 00:00 in UTC+1.
    assert hour_of_week(MONDAY - timedelta(hours=1), cet) == 0
    india = timezone(timedelta(hours=5, minutes=30))
    assert hour_of_week(MONDAY, india) == 5 * 4 + 2


def test_decay_weight_has_a_three_week_half_life() -> None:
    assert decay_weight(timedelta(0)) == 1.0
    assert decay_weight(timedelta(weeks=3)) == pytest.approx(0.5)
    assert decay_weight(timedelta(weeks=6)) == pytest.approx(0.25)
    assert decay_weight(-timedelta(days=1)) == 1.0


def test_weighted_quantile() -> None:
    equal = [(v, 1.0) for v in (5.0, 1.0, 3.0, 2.0, 4.0)]
    assert weighted_quantile(equal, 0.5) == 3.0
    assert weighted_quantile(equal, 0.9) == 5.0
    assert weighted_quantile(equal, 0.8) == 4.0  # exactly 4/5 of the weight
    assert weighted_quantile(equal, 0.0) == 1.0
    assert weighted_quantile(equal, 1.0) == 5.0
    # A heavy sample pulls the quantile to itself; zero weights are ignored.
    assert weighted_quantile([(0.0, 1.0), (10.0, 3.0), (99.0, 0.0)], 0.5) == 10.0
    assert weighted_quantile([(0.0, 3.0), (10.0, 1.0)], 0.5) == 0.0
    assert weighted_quantile([(0.0, 3.0), (10.0, 1.0)], 0.9) == 10.0
    with pytest.raises(ValueError, match="at least one sample"):
        weighted_quantile([], 0.5)
    with pytest.raises(ValueError, match="at least one sample"):
        weighted_quantile([(1.0, 0.0)], 0.5)
    with pytest.raises(ValueError, match="q must be"):
        weighted_quantile(equal, 1.5)


def test_no_data_prior_fills_a_whole_window() -> None:
    assert no_data_pct(FIVE_HOUR) == pytest.approx(5.0)
    assert no_data_pct(SEVEN_DAY) * SLOTS_PER_WEEK == pytest.approx(100.0)


def test_dense_observations_fill_zeros_and_sum_per_slot() -> None:
    usage = {
        MONDAY + timedelta(minutes=3): 1.0,
        MONDAY + timedelta(minutes=10): 0.5,
        MONDAY + timedelta(hours=2): 9.0,
    }
    obs = dense_observations(usage, MONDAY, MONDAY + timedelta(hours=1))
    assert [o.slot_start for o in obs] == [MONDAY + k * SLOT for k in range(4)]
    assert [o.manual_pct for o in obs] == [1.5, 0.0, 0.0, 0.0]


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="manual_pct"):
        SlotObservation(MONDAY, -0.1)
    with pytest.raises(ValueError, match="manual_pct"):
        SlotObservation(MONDAY, float("nan"))
    with pytest.raises(ValueError, match="kernel"):
        BaselineConfig(kernel=())
    with pytest.raises(ValueError, match="half_life"):
        BaselineConfig(half_life=timedelta(0))
    with pytest.raises(ValueError, match="template needs"):
        fit_baseline(FIVE_HOUR, [], FakeClock(MONDAY), template=[1.0] * 10)
    with pytest.raises(ValueError, match="template values"):
        fit_baseline(FIVE_HOUR, [], FakeClock(MONDAY), template=[-1.0] * SLOTS_PER_WEEK)


# ------------------------------------------------------------------------------------------ quantiles


def test_steady_weekly_pattern_is_learned() -> None:
    nine = 9 * 4  # Monday 09:00

    def value(_w: int, i: int) -> float:
        return 4.0 if i == nine else 0.0

    model = fit_baseline(FIVE_HOUR, _weeks(4, value), _clock_after(4))
    assert not model.cold_start
    assert model.history_days == pytest.approx(28.0)
    assert set(model.basis) == {SlotBasis.HISTORY}
    f = model.slot(MONDAY + 4 * WEEK + timedelta(hours=9, minutes=7))
    assert (f.p50_pct, f.p90_pct, f.basis) == (4.0, 4.0, SlotBasis.HISTORY)
    assert model.slot(MONDAY + timedelta(hours=14)).p90_pct == 0.0


def test_p90_is_the_bad_week_and_p50_the_typical_one() -> None:
    nine = 9 * 4

    def value(w: int, i: int) -> float:
        if i != nine:
            return 0.0
        return 12.0 if w == 9 else 2.0  # one heavy week out of ten (the most recent one)

    model = fit_baseline(FIVE_HOUR, _weeks(10, value), _clock_after(10), config=BaselineConfig(kernel=(1.0,)))
    f = model.slot(MONDAY + timedelta(hours=9))
    assert f.p50_pct == 2.0
    assert f.p90_pct == 12.0


def test_decay_lets_recent_habits_win() -> None:
    """Four old weeks at 10%, then three recent weeks at 0%: unweighted the median is 10, decayed it is 0."""

    def value(w: int, _i: int) -> float:
        return 10.0 if w < 4 else 0.0

    obs = _weeks(7, value)
    decayed = fit_baseline(FIVE_HOUR, obs, _clock_after(7))
    flat = fit_baseline(
        FIVE_HOUR, obs, _clock_after(7), config=BaselineConfig(half_life=timedelta(weeks=10_000))
    )
    assert decayed.p50[0] == 0.0
    assert flat.p50[0] == 10.0
    # P90 still remembers the old heavy weeks: being cautious costs agent time, not the user's time.
    assert decayed.p90[0] == 10.0


def test_neighbour_smoothing_spreads_a_spike() -> None:
    spike = 9 * 4 + 1  # Monday 09:15

    def value(_w: int, i: int) -> float:
        return 8.0 if i == spike else 0.0

    obs = _weeks(4, value)
    smoothed = fit_baseline(FIVE_HOUR, obs, _clock_after(4))
    raw = fit_baseline(FIVE_HOUR, obs, _clock_after(4), config=BaselineConfig(kernel=(1.0,)))
    for neighbour in (spike - 1, spike + 1):
        assert raw.p90[neighbour] == 0.0
        assert smoothed.p90[neighbour] == 8.0  # covered in case the user starts a slot earlier or later
        assert smoothed.p50[neighbour] == 0.0  # but the typical value stays the neighbour's own
    assert smoothed.p90[spike - 2] == 0.0
    assert smoothed.p90[spike + 2] == 0.0
    assert smoothed.p90[spike] == raw.p90[spike] == 8.0


def test_smoothing_wraps_around_the_week() -> None:
    def value(_w: int, i: int) -> float:
        return 3.0 if i == 0 else 0.0

    model = fit_baseline(FIVE_HOUR, _weeks(3, value), _clock_after(3))
    assert model.p90[SLOTS_PER_WEEK - 1] == 3.0
    assert model.p90[1] == 3.0


def test_jittered_start_is_covered_in_both_slots() -> None:
    nine, nine_fifteen = 9 * 4, 9 * 4 + 1

    def value(w: int, i: int) -> float:
        return 5.0 if i == (nine if w % 2 else nine_fifteen) else 0.0

    model = fit_baseline(FIVE_HOUR, _weeks(4, value), _clock_after(4))
    assert model.p90[nine] == 5.0
    assert model.p90[nine_fifteen] == 5.0


def test_local_time_zone_aligns_slots_with_the_wall_clock() -> None:
    """A user in UTC+1 who works at 09:00 local: forecasts land on 08:00 UTC."""
    cet = timezone(timedelta(hours=1))
    local_nine_utc = 8 * 4

    def value(_w: int, i: int) -> float:
        return 6.0 if i == local_nine_utc else 0.0

    model = fit_baseline(FIVE_HOUR, _weeks(3, value), _clock_after(3), tz=cet)
    assert model.p90[9 * 4] == 6.0  # indexed by local hour-of-week
    assert model.slot(datetime(2026, 10, 26, 8, 0, tzinfo=UTC)).p90_pct == 6.0


# ----------------------------------------------------------------------------------------- cold start


def test_cold_start_uses_the_max_of_comparable_slots() -> None:
    """Five days of data: Saturday 10:00 was never seen, so it gets the highest 10:00 usage of any day."""
    ten = 10 * 4

    def value(_w: int, i: int) -> float:
        day, tod = divmod(i, SLOTS_PER_DAY)
        return float(day + 1) if tod == ten else 0.0  # Mon 1%, Tue 2%, …, Fri 5%

    obs = [o for o in _weeks(1, value) if o.slot_start < MONDAY + timedelta(days=5)]
    model = fit_baseline(FIVE_HOUR, obs, FakeClock(MONDAY + timedelta(days=5)))
    assert model.cold_start
    assert model.history_days == pytest.approx(5.0)
    saturday_ten = 5 * SLOTS_PER_DAY + ten
    assert model.basis[saturday_ten] == SlotBasis.COLD_START
    assert model.p90[saturday_ten] == 5.0
    assert model.p90[0 * SLOTS_PER_DAY + ten] == 5.0  # Monday's own 1% is not trusted either
    assert model.p90[saturday_ten + 1] == 5.0  # neighbour radius applies to comparable slots
    # P50 is the weighted median of comparable 10:00 values (1..5) plus the 09:45/10:15 zeros at 0.25.
    assert model.p50[saturday_ten] == 2.0
    assert model.p90[saturday_ten + 2] == 0.0


def test_cold_start_holds_until_two_weeks_of_observed_slots() -> None:
    """Three calendar weeks with only half the slots observed is 10.5 days of data: still cold."""
    obs = [o for o in _weeks(3, lambda _w, _i: 1.0) if (o.slot_start - MONDAY) // SLOT % 2 == 0]
    model = fit_baseline(FIVE_HOUR, obs, _clock_after(3))
    assert model.history_days == pytest.approx(10.5)
    assert model.cold_start
    assert set(model.basis) == {SlotBasis.COLD_START}
    warm = fit_baseline(FIVE_HOUR, _weeks(2, lambda _w, _i: 1.0), _clock_after(2))
    assert not warm.cold_start


def test_unseen_time_of_day_without_template_assumes_the_whole_window() -> None:
    """Only 09:00-17:00 observed: night slots get the no-data prior, which leaves agents nothing."""
    obs = [
        o
        for o in _weeks(1, lambda _w, _i: 0.5)
        if 9 <= o.slot_start.hour < 17 and o.slot_start < MONDAY + timedelta(days=3)
    ]
    model = fit_baseline(FIVE_HOUR, obs, FakeClock(MONDAY + timedelta(days=3)))
    night = 3 * 4
    assert model.basis[night] == SlotBasis.NO_DATA
    assert model.p90[night] == model.p50[night] == no_data_pct(FIVE_HOUR)
    window = model.forecast(MONDAY + 3 * WEEK, MONDAY + 3 * WEEK + timedelta(hours=5))
    assert sum(f.p90_pct for f in window) == pytest.approx(100.0)


def test_no_data_at_all() -> None:
    model = fit_baseline(SEVEN_DAY, [], FakeClock(MONDAY))
    assert model.cold_start
    assert model.history_days == 0.0
    assert set(model.basis) == {SlotBasis.NO_DATA}
    assert sum(model.p90) == pytest.approx(100.0)


def test_template_replaces_the_no_data_prior() -> None:
    template = [2.0 if 9 * 4 <= i % SLOTS_PER_DAY < 17 * 4 else 0.0 for i in range(SLOTS_PER_WEEK)]
    model = fit_baseline(FIVE_HOUR, [], FakeClock(MONDAY), template=template)
    assert set(model.basis) == {SlotBasis.TEMPLATE}
    assert list(model.p90) == template
    assert list(model.p50) == template


def test_template_never_lowers_the_prior_below_data() -> None:
    template = [1.0] * SLOTS_PER_WEEK
    obs = [SlotObservation(MONDAY + timedelta(hours=10), 7.0)]
    model = fit_baseline(FIVE_HOUR, obs, FakeClock(MONDAY + timedelta(days=1)), template=template)
    ten = 10 * 4
    assert model.basis[ten] == SlotBasis.COLD_START
    assert model.p90[ten] == 7.0  # data above the template wins
    assert model.p90[3 * SLOTS_PER_DAY + ten] == 7.0  # Thursday 10:00 is comparable
    assert model.basis[3 * 4] == SlotBasis.TEMPLATE
    assert model.p90[3 * 4] == 1.0


def test_template_is_ignored_once_warm() -> None:
    model = fit_baseline(
        FIVE_HOUR, _weeks(3, lambda _w, _i: 0.0), _clock_after(3), template=[9.0] * SLOTS_PER_WEEK
    )
    assert set(model.basis) == {SlotBasis.HISTORY}
    assert max(model.p90) == 0.0


def test_warm_model_falls_back_per_slot_for_gaps() -> None:
    """Three weeks of data, but Wednesday 03:00 was only observed once: that slot uses the cold-start rule."""
    gap = 2 * SLOTS_PER_DAY + 3 * 4

    def value(_w: int, i: int) -> float:
        return 4.0 if i % SLOTS_PER_DAY == 3 * 4 + 1 else 0.0  # 03:15 every day

    obs = [
        o
        for o in _weeks(3, value)
        if not (hour_of_week(o.slot_start) == gap and o.slot_start >= MONDAY + WEEK)
    ]
    model = fit_baseline(FIVE_HOUR, obs, _clock_after(3))
    assert not model.cold_start
    assert model.basis[gap] == SlotBasis.COLD_START
    assert model.p90[gap] == 4.0
    assert model.basis[gap + SLOTS_PER_DAY] == SlotBasis.HISTORY


# ------------------------------------------------------------------------------------- purity / edges


def test_incomplete_and_future_slots_are_ignored() -> None:
    now = MONDAY + timedelta(hours=10, minutes=5)
    obs = [
        SlotObservation(MONDAY + timedelta(hours=9, minutes=45), 1.0),
        SlotObservation(MONDAY + timedelta(hours=10), 50.0),  # still running
        SlotObservation(MONDAY + timedelta(days=1), 50.0),  # in the future
    ]
    model = fit_baseline(FIVE_HOUR, obs, FakeClock(now))
    assert max(model.p90) == pytest.approx(max(1.0, no_data_pct(FIVE_HOUR)))
    assert model.history_days == pytest.approx(15 / (24 * 60))


def test_duplicate_observations_of_a_slot_are_summed() -> None:
    obs = [
        SlotObservation(MONDAY + timedelta(minutes=1), 1.0),
        SlotObservation(MONDAY + timedelta(minutes=14), 2.0),
    ]
    model = fit_baseline(FIVE_HOUR, obs, FakeClock(MONDAY + timedelta(hours=1)))
    assert model.p90[0] == 3.0
    assert model.history_days == pytest.approx(15 / (24 * 60))


def test_fit_is_deterministic_and_stamped_with_the_clock() -> None:
    obs = _weeks(2, lambda w, i: float((w * 7 + i) % 5))
    clock = _clock_after(2)
    a = fit_baseline(FIVE_HOUR, obs, clock)
    b = fit_baseline(FIVE_HOUR, list(reversed(obs)), clock)
    assert a == b
    assert a.fitted_at == clock.now()
    clock.advance(timedelta(weeks=3))
    assert fit_baseline(FIVE_HOUR, obs, clock).fitted_at == clock.now()


def test_forecast_covers_the_range_slot_by_slot() -> None:
    model = fit_baseline(
        FIVE_HOUR, _weeks(2, lambda _w, i: float(i)), _clock_after(2), config=BaselineConfig(kernel=(1.0,))
    )
    start = MONDAY + 2 * WEEK + timedelta(hours=1, minutes=7)
    out = model.forecast(start, start + timedelta(hours=1))
    assert [f.slot_start for f in out] == [
        MONDAY + 2 * WEEK + timedelta(hours=1) + k * SLOT for k in range(5)
    ]
    assert [f.p90_pct for f in out] == [4.0, 5.0, 6.0, 7.0, 8.0]
    assert all(f.bucket == FIVE_HOUR for f in out)
    assert model.forecast(start, start) == []


@settings(max_examples=40, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(0, 4 * SLOTS_PER_WEEK - 1), st.floats(0, 30, allow_nan=False)),
        max_size=200,
    ),
    st.booleans(),
)
def test_quantiles_are_ordered_and_bounded(points: list[tuple[int, float]], use_template: bool) -> None:
    obs = [SlotObservation(MONDAY + k * SLOT, v) for k, v in points]
    template = [0.7] * SLOTS_PER_WEEK if use_template else None
    model = fit_baseline(FIVE_HOUR, obs, _clock_after(4), template=template)
    sums: dict[int, float] = {}
    for k, v in points:
        sums[k] = sums.get(k, 0.0) + v
    ceiling = max([*sums.values(), 0.7 if use_template else no_data_pct(FIVE_HOUR)])
    for lo, hi in zip(model.p50, model.p90, strict=True):
        assert 0.0 <= lo <= hi <= ceiling + 1e-9
