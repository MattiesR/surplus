from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from surplus.collector.reset_watcher import (
    detect_shifts,
    next_probe_at,
    project_reset,
    reset_times_fresh,
)
from surplus.domain.models import LimitReading, ReadingSource

from conftest import T0

FIVE_RESET = datetime(2026, 11, 26, 0, 30, tzinfo=UTC)
WEEK_RESET = datetime(2026, 12, 2, 19, 0, tzinfo=UTC)


def reading(bucket: str, resets_at: datetime | None, ts: datetime = T0) -> LimitReading:
    return LimitReading(ts=ts, bucket=bucket, used_pct=10, resets_at=resets_at, source=ReadingSource.PROBE)


def test_project_reset_future_is_unchanged() -> None:
    assert project_reset(FIVE_RESET, "five_hour", T0) == FIVE_RESET


def test_project_reset_rolls_forward_by_period() -> None:
    now = FIVE_RESET + timedelta(hours=7)
    assert project_reset(FIVE_RESET, "five_hour", now) == FIVE_RESET + timedelta(hours=10)
    assert project_reset(WEEK_RESET, "seven_day", WEEK_RESET) == WEEK_RESET + timedelta(days=7)


@given(st.integers(min_value=0, max_value=60 * 24 * 30))
def test_project_reset_properties(minutes: int) -> None:
    now = FIVE_RESET + timedelta(minutes=minutes)
    nxt = project_reset(FIVE_RESET, "five_hour", now)
    assert now < nxt <= now + timedelta(hours=5)
    assert (nxt - FIVE_RESET) % timedelta(hours=5) == timedelta(0)


def test_first_probe_is_immediate() -> None:
    assert next_probe_at(T0, None, []) == T0


def test_probe_every_15_minutes() -> None:
    latest = [reading("five_hour", FIVE_RESET), reading("seven_day", WEEK_RESET)]
    assert next_probe_at(T0, T0, latest) == T0 + timedelta(minutes=15)


def test_probe_shortly_after_expected_reset() -> None:
    last = FIVE_RESET - timedelta(minutes=10)
    latest = [reading("five_hour", FIVE_RESET)]
    assert next_probe_at(last, last, latest) == FIVE_RESET + timedelta(minutes=3)


def test_confirmed_reset_does_not_retrigger() -> None:
    last = FIVE_RESET + timedelta(minutes=4)  # probed after the reset was confirmed
    latest = [reading("five_hour", FIVE_RESET)]
    assert next_probe_at(last, last, latest) == last + timedelta(minutes=15)


def test_overdue_probe_is_now() -> None:
    now = T0 + timedelta(hours=2)
    assert next_probe_at(now, T0, []) == now


def test_detect_shift_beyond_tolerance() -> None:
    expected = {"five_hour": FIVE_RESET}
    moved = reading("five_hour", FIVE_RESET + timedelta(minutes=30))
    (shift,) = detect_shifts(expected, [moved])
    assert shift.delta == timedelta(minutes=30)


def test_small_drift_and_projected_rollover_are_not_shifts() -> None:
    expected = {"five_hour": FIVE_RESET, "seven_day": WEEK_RESET}
    after_reset = FIVE_RESET + timedelta(minutes=3)
    new = [
        reading("five_hour", FIVE_RESET + timedelta(hours=5, minutes=2), ts=after_reset),  # next grid slot
        reading("seven_day", WEEK_RESET - timedelta(minutes=4)),
        reading("seven_day_opus", WEEK_RESET),  # unknown bucket: nothing to compare
    ]
    assert detect_shifts(expected, new) == []


def test_reset_freshness() -> None:
    assert not reset_times_fresh(None, T0)
    assert reset_times_fresh(T0 - timedelta(minutes=59), T0)
    assert not reset_times_fresh(T0 - timedelta(minutes=61), T0)
