"""User-activity detector (plan §6.3), driven by a FakeClock."""

from __future__ import annotations

from datetime import timedelta

from surplus.clock import FakeClock
from surplus.predictor.activity import ReadingRise, user_activity

from conftest import T0


def rise(clock: FakeClock, minutes_ago: float, rise_pct: float, explained: float | None) -> ReadingRise:
    end = clock.now() - timedelta(minutes=minutes_ago)
    return ReadingRise(end - timedelta(minutes=15), end, rise_pct, explained)


def test_idle_without_signs() -> None:
    clock = FakeClock(T0)
    a = user_activity(clock.now(), None, [])
    assert not a.active and a.reason is None


def test_recent_manual_event_is_active_until_it_ages_out() -> None:
    clock = FakeClock(T0)
    seen = clock.now()
    clock.advance(timedelta(minutes=4))
    a = user_activity(clock.now(), seen, [])
    assert a.active and a.last_seen == seen and "4 min ago" in (a.reason or "")
    clock.advance(timedelta(minutes=7))  # 11 min after the event
    assert not user_activity(clock.now(), seen, []).active


def test_unexplained_jump_means_invisible_use() -> None:
    clock = FakeClock(T0)
    a = user_activity(clock.now(), None, [rise(clock, 5, rise_pct=3.0, explained=0.5)])
    assert a.active and "2.5 pp" in (a.reason or "")


def test_agent_spend_is_explained_and_not_the_user() -> None:
    clock = FakeClock(T0)
    assert not user_activity(clock.now(), None, [rise(clock, 5, rise_pct=4.0, explained=3.8)]).active


def test_without_a_cost_model_every_rise_counts() -> None:
    clock = FakeClock(T0)
    assert user_activity(clock.now(), None, [rise(clock, 5, rise_pct=1.0, explained=None)]).active


def test_old_jumps_are_ignored_and_newest_sign_wins() -> None:
    clock = FakeClock(T0)
    old_jump = rise(clock, 45, rise_pct=5.0, explained=0.0)
    assert not user_activity(clock.now(), None, [old_jump]).active
    manual_at = clock.now() - timedelta(minutes=8)
    newer_jump = rise(clock, 2, rise_pct=2.0, explained=0.0)
    a = user_activity(clock.now(), manual_at, [old_jump, newer_jump])
    assert a.active and a.last_seen == newer_jump.end
