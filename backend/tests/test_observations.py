"""Per-slot user usage from readings + costed events (ADR 0011)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from surplus.domain.models import LimitReading, ReadingSource
from surplus.predictor.observations import CostedEvent, user_slot_observations

T = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)  # a Monday, on a slot boundary
RESET = T + timedelta(hours=3)


def reading(
    minutes: float, pct: float, reset: datetime | None = RESET, source: ReadingSource = ReadingSource.PROBE
) -> LimitReading:
    return LimitReading(
        ts=T + timedelta(minutes=minutes), bucket="five_hour", used_pct=pct, resets_at=reset, source=source
    )


def ev(minutes: float, pct: float | None, agent: bool = False) -> CostedEvent:
    return CostedEvent(T + timedelta(minutes=minutes), pct, agent)


NOW = T + timedelta(hours=2)


def as_dict(obs: list) -> dict[int, float]:  # type: ignore[type-arg]
    return {int((o.slot_start - T) / timedelta(minutes=15)): round(o.manual_pct, 6) for o in obs}


def test_manual_events_land_in_their_slots_and_quiet_slots_are_zero() -> None:
    obs = user_slot_observations("five_hour", [reading(0, 10), reading(60, 13)], [ev(5, 1), ev(40, 2)], NOW)
    assert as_dict(obs) == {0: 1.0, 1: 0.0, 2: 2.0, 3: 0.0}


def test_agent_spend_is_not_user_usage_but_explains_the_rise() -> None:
    obs = user_slot_observations(
        "five_hour", [reading(0, 10), reading(30, 15)], [ev(5, 1), ev(20, 4, agent=True)], NOW
    )
    assert as_dict(obs) == {0: 1.0, 1: 0.0}


def test_unexplained_rise_is_spread_as_invisible_user_usage() -> None:
    obs = user_slot_observations("five_hour", [reading(0, 10), reading(30, 14)], [ev(5, 1)], NOW)
    # 3 pp invisible over two slots
    assert as_dict(obs) == {0: pytest.approx(2.5), 1: pytest.approx(1.5)}


def test_without_a_cost_model_the_whole_rise_counts_as_the_user() -> None:
    obs = user_slot_observations(
        "five_hour", [reading(0, 10), reading(30, 16)], [ev(5, None, agent=True)], NOW
    )
    assert sum(o.manual_pct for o in obs) == pytest.approx(6.0)


def test_gaps_resets_and_manual_readings_leave_slots_unobserved() -> None:
    readings = [
        reading(0, 10),
        reading(30, 12),
        reading(45, 1, reset=RESET + timedelta(hours=5)),  # a reset happened in between
        reading(60, 20, reset=None, source=ReadingSource.MANUAL),  # manual: no reset known
    ]
    obs = user_slot_observations("five_hour", readings, [], NOW)
    assert sorted(as_dict(obs)) == [0, 1]
    far = [reading(0, 10), reading(7 * 60, 30)]  # more than 6 h apart
    assert user_slot_observations("five_hour", far, [], T + timedelta(hours=8)) == []


def test_running_and_half_covered_slots_are_left_out() -> None:
    obs = user_slot_observations(
        "five_hour", [reading(0, 10), reading(20, 11)], [], T + timedelta(minutes=25)
    )
    # slot 0 is completed and covered; slot 1 (15-30) is still running and only a third covered
    assert sorted(as_dict(obs)) == [0]


def test_other_buckets_are_ignored() -> None:
    weekly = LimitReading(ts=T, bucket="seven_day", used_pct=5, resets_at=RESET, source=ReadingSource.PROBE)
    assert user_slot_observations("five_hour", [weekly], [], NOW) == []


def test_timezone_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    from zoneinfo import ZoneInfo

    from surplus.config import resolve_timezone

    assert resolve_timezone("Europe/Brussels") == ZoneInfo("Europe/Brussels")
    monkeypatch.setenv("TZ", "America/New_York")
    assert resolve_timezone("") == ZoneInfo("America/New_York")
    assert resolve_timezone("Not/AZone") == ZoneInfo("America/New_York")  # bad name: fall through
