"""Cost model persistence and the collector's nightly refit (plan §5.3): R² and coefficients are stored."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import FakeClock
from surplus.collector.service import COST_MODEL_REFIT_EVERY, Collector
from surplus.collector.store import (
    cost_model_inputs,
    insert_events,
    insert_readings,
    latest_cost_model,
    save_cost_model,
)
from surplus.config import Paths, Settings
from surplus.db.models import CostModelRow
from surplus.domain.events import EventBus
from surplus.domain.models import (
    FIVE_HOUR,
    LimitReading,
    ReadingSource,
    TokenCounts,
    UsageEvent,
    UsageSourceKind,
)
from surplus.predictor.cost_model import (
    PRICE_RATIO_WEIGHTS,
    CoefficientSource,
    FitWindow,
    fit_cost_model,
)

from conftest import T0

OPUS = 1e-5  # % of the 5-h limit per weighted opus token


def _seed(sm: sessionmaker[Session], noise: bool = False) -> None:
    """Two days of 15-minute readings ending at T0, with opus usage that costs exactly OPUS per token."""
    rng = np.random.default_rng(2)
    start = T0 - timedelta(days=2)
    readings, events = [], []
    used, reset = 0.0, start + timedelta(hours=5)
    for k in range(4 * 48):
        ts = start + timedelta(minutes=15 * k)
        if ts >= reset:
            used, reset = 0.0, reset + timedelta(hours=5)
        shown = float(rng.uniform(0, 100)) if noise else round(used, 6)
        readings.append(
            LimitReading(ts=ts, bucket=FIVE_HOUR, used_pct=shown, resets_at=reset, source=ReadingSource.PROBE)
        )
        t = TokenCounts(input=int(rng.integers(1, 2000)), output=int(rng.integers(0, 5000)))
        events.append(
            UsageEvent(
                message_id=f"m{k}",
                ts=ts + timedelta(minutes=3),
                source=UsageSourceKind.CC_LOG,
                session_id="s",
                model="claude-opus-5-5",
                tokens=t,
            )
        )
        used += OPUS * PRICE_RATIO_WEIGHTS.apply(t)
    with sm.begin() as session:
        insert_readings(session, readings)
        insert_events(session, events)


def _collector(sm: sessionmaker[Session], paths: Paths, clock: FakeClock) -> Collector:
    return Collector(sm, paths, Settings(), clock, EventBus())


def test_save_and_load_roundtrip(sm: sessionmaker[Session], clock: FakeClock) -> None:
    w = [
        FitWindow(FIVE_HOUR, T0, T0, OPUS * 100 * k, {"opus": TokenCounts(input=100 * k)})
        for k in range(1, 10)
    ]
    fit = fit_cost_model(FIVE_HOUR, w, clock)
    with sm.begin() as session:
        assert save_cost_model(session, fit) == 4
    with sm() as session:
        loaded = latest_cost_model(session, FIVE_HOUR)
        assert latest_cost_model(session, "seven_day") is None
    assert loaded == fit


def test_empty_fit_stores_nothing(sm: sessionmaker[Session], clock: FakeClock) -> None:
    with sm.begin() as session:
        assert save_cost_model(session, fit_cost_model(FIVE_HOUR, [], clock)) == 0
    with sm() as session:
        assert latest_cost_model(session, FIVE_HOUR) is None


def test_cost_model_inputs_filters_bucket_and_range(sm: sessionmaker[Session]) -> None:
    _seed(sm)
    with sm() as session:
        readings, events = cost_model_inputs(session, FIVE_HOUR, T0 - timedelta(hours=1), T0)
        other, _ = cost_model_inputs(session, "seven_day", T0 - timedelta(days=3), T0)
    assert len(readings) == 4
    assert len(events) == 4
    assert other == []


def test_refit_recovers_and_stores_r2(sm: sessionmaker[Session], paths: Paths, clock: FakeClock) -> None:
    _seed(sm)
    collector = _collector(sm, paths, clock)
    assert collector.cost_model_due()
    (fit,) = collector.refit_cost_models()
    assert not fit.low_confidence
    assert fit.coefficients["opus"].pct_per_weighted_token == pytest.approx(OPUS, rel=1e-3)
    assert fit.coefficients["opus"].source is CoefficientSource.FIT
    stored = collector.cost_model(FIVE_HOUR)
    assert stored is not None
    assert stored.r2 == pytest.approx(fit.r2)
    assert stored.r2 is not None
    assert stored.r2 > 0.99
    with sm() as session:
        rows = list(session.scalars(select(CostModelRow)))
    assert {r.model for r in rows} == {"haiku", "sonnet", "opus", "other"}
    assert all(r.r2 == pytest.approx(fit.r2) and not r.low_confidence for r in rows)


def test_refit_flags_low_r2(sm: sessionmaker[Session], paths: Paths, clock: FakeClock) -> None:
    _seed(sm, noise=True)
    collector = _collector(sm, paths, clock)
    fits = collector.refit_cost_models()
    assert len(fits) == 1
    stored = collector.cost_model(FIVE_HOUR)
    assert stored is not None
    assert stored.low_confidence
    assert stored.r2 is not None
    assert stored.r2 < 0.7


def test_refit_is_nightly(sm: sessionmaker[Session], paths: Paths, clock: FakeClock) -> None:
    collector = _collector(sm, paths, clock)
    assert collector.cost_model_due()
    assert collector.refit_cost_models() == []  # no readings: nothing stored, but the attempt counts
    assert not collector.cost_model_due()
    clock.advance(COST_MODEL_REFIT_EVERY - timedelta(minutes=1))
    assert not collector.cost_model_due()
    clock.advance(timedelta(minutes=1))
    assert collector.cost_model_due()
