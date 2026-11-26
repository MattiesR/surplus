"""The live plan: inputs gathered from the store, the pure planner, and the /api/plan endpoint."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from surplus.api.app import create_app
from surplus.api.auth import load_or_create_token
from surplus.clock import FakeClock
from surplus.collector.service import Collector
from surplus.collector.store import insert_readings
from surplus.config import Paths, Settings
from surplus.domain.events import EventBus
from surplus.domain.models import FIVE_HOUR, SEVEN_DAY, LimitReading, ReadingSource
from surplus.optimizer.budget import WEEKLY_PER_FIVE_PRIOR, weekly_per_five_from_fits
from surplus.planning import PlanService

from conftest import T0


def test_conversion_uses_the_largest_fitted_ratio() -> None:
    c = weekly_per_five_from_fits({"opus": 2e-6, "sonnet": 1e-6}, {"opus": 2e-7, "sonnet": 3e-7}, True)
    assert c.source == "cost model" and c.weekly_per_five == pytest.approx(0.3)
    assert "sonnet" in c.reason


@pytest.mark.parametrize(
    ("five", "weekly", "trust"),
    [
        ({"opus": 1e-6}, {"opus": 1e-7}, False),
        (None, {"opus": 1e-7}, True),
        ({"opus": 1e-6}, {"sonnet": 1e-7}, True),  # no family in both
        ({"opus": 0.0}, {"opus": 1e-7}, True),
    ],
)
def test_conversion_falls_back_to_the_prior(
    five: dict[str, float] | None, weekly: dict[str, float] | None, trust: bool
) -> None:
    c = weekly_per_five_from_fits(five, weekly, trust)
    assert c.source == "prior" and c.weekly_per_five == WEEKLY_PER_FIVE_PRIOR


def test_conversion_is_clamped() -> None:
    assert weekly_per_five_from_fits({"opus": 1e-9}, {"opus": 1e-6}, True).weekly_per_five == 1.0


def _readings(sm: sessionmaker[Session], now: FakeClock, five: float = 20.0, week: float = 30.0) -> None:
    t = now.now()
    with sm.begin() as s:
        insert_readings(
            s,
            [
                LimitReading(
                    ts=t,
                    bucket=FIVE_HOUR,
                    used_pct=five,
                    resets_at=t + timedelta(hours=3),
                    source=ReadingSource.PROBE,
                ),
                LimitReading(
                    ts=t,
                    bucket=SEVEN_DAY,
                    used_pct=week,
                    resets_at=t + timedelta(days=2, hours=1),
                    source=ReadingSource.PROBE,
                ),
            ],
        )


def _service(sm: sessionmaker[Session], paths: Paths, clock: FakeClock) -> PlanService:
    return PlanService(sm, clock, Collector(sm, paths, Settings(), clock, EventBus()))


def test_no_plan_without_reset_times(sm: sessionmaker[Session], paths: Paths, clock: FakeClock) -> None:
    live = _service(sm, paths, clock).current()
    assert live.plan is None and live.unavailable and "reset time" in live.unavailable


def test_plan_from_live_readings(sm: sessionmaker[Session], paths: Paths, clock: FakeClock) -> None:
    _readings(sm, clock)
    live = _service(sm, paths, clock).current()
    p = live.plan
    assert p is not None and live.unavailable is None
    # Windows: the current one ends at the reported reset, then every 5 h until the weekly reset.
    assert p.windows[0].current and p.windows[0].end == T0 + timedelta(hours=3)
    assert p.windows[1].start == T0 + timedelta(hours=3)
    assert p.windows[-1].end == T0 + timedelta(days=2, hours=1)
    assert all(0 <= w.allocation <= w.capacity + 1e-9 for w in p.windows)
    # No history: cold start with the no-data prior, low confidence, the conversion prior.
    assert live.conversion is not None and live.conversion.source == "prior"
    assert live.confidence is not None and live.confidence.value < 0.5
    assert len(live.forecast) == 4 * 49


def test_plan_resyncs_when_claude_moves_the_reset(
    sm: sessionmaker[Session], paths: Paths, clock: FakeClock
) -> None:
    svc = _service(sm, paths, clock)
    _readings(sm, clock)
    assert svc.current().drift is None
    clock.advance(timedelta(minutes=15))
    with sm.begin() as s:
        insert_readings(
            s,
            [
                LimitReading(
                    ts=clock.now(),
                    bucket=FIVE_HOUR,
                    used_pct=25,
                    resets_at=T0 + timedelta(hours=3, minutes=20),
                    source=ReadingSource.PROBE,
                )
            ],
        )
    live = svc.current()
    assert live.drift == timedelta(minutes=20)
    assert live.plan is not None and live.plan.windows[0].end == T0 + timedelta(hours=3, minutes=20)


def test_plan_endpoint(paths: Paths, clock: FakeClock) -> None:
    app = create_app(paths, Settings(), clock, background=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        token = load_or_create_token(paths.api_token_file)
        h = {"Authorization": f"Bearer {token}"}
        empty = c.get("/api/plan", headers=h).json()
        assert empty["available"] is False and empty["windows"] == []
        _readings(app.state.collector.sm, clock)
        body = c.get("/api/plan", headers=h).json()
    assert body["available"] and body["windows"][0]["current"] and body["weekly"]["reasons"]
    assert body["conversion_source"] == "prior" and len(body["forecast"]) > 0
