"""Forecast confidence (plan §6.4): bounds, the anchor points, and monotonicity in every input."""

from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from surplus.predictor.confidence import (
    NO_FIT_FACTOR,
    ConfidenceInputs,
    confidence,
    cost_model_factor,
    freshness_factor,
    history_factor,
)

days = st.floats(min_value=0, max_value=200, allow_nan=False)
r2 = st.one_of(st.none(), st.floats(min_value=-5, max_value=1, allow_nan=False))
age = st.one_of(st.none(), st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=3)))


def test_perfect_inputs_give_full_confidence() -> None:
    c = confidence(ConfidenceInputs(30, 0.95, False, timedelta(minutes=5)))
    assert c.value == 1.0 and c.reasons == ()


def test_anchor_points() -> None:
    assert history_factor(0) == 0 and history_factor(14) == pytest.approx(0.5) and history_factor(40) == 1
    assert cost_model_factor(None, False) == NO_FIT_FACTOR
    assert cost_model_factor(0.95, False) == 1.0
    assert cost_model_factor(0.95, True) == pytest.approx(0.6)  # a flagged fit never counts as good
    assert cost_model_factor(0.2, False) == pytest.approx(0.5)
    assert freshness_factor(None) == 0 and freshness_factor(timedelta(minutes=10)) == 1
    assert freshness_factor(timedelta(hours=3)) == 0


def test_reasons_name_what_is_missing() -> None:
    c = confidence(ConfidenceInputs(3, None, False, None))
    assert c.value == 0
    assert any("history" in r for r in c.reasons)
    assert "no cost-model fit yet" in c.reasons and "no authoritative reading" in c.reasons


@given(days, r2, st.booleans(), age)
def test_bounded(d: float, r: float | None, low: bool, a: timedelta | None) -> None:
    v = confidence(ConfidenceInputs(d, r, low, a)).value
    assert 0.0 <= v <= 1.0


@given(days, days, r2, st.booleans(), age)
def test_more_history_never_lowers_confidence(
    d1: float, d2: float, r: float | None, low: bool, a: timedelta | None
) -> None:
    lo, hi = sorted((d1, d2))
    assert (
        confidence(ConfidenceInputs(lo, r, low, a)).value <= confidence(ConfidenceInputs(hi, r, low, a)).value
    )


@given(days, st.floats(-5, 1), st.floats(-5, 1), st.booleans(), age)
def test_better_fit_never_lowers_confidence(
    d: float, r1: float, r2_: float, low: bool, a: timedelta | None
) -> None:
    lo, hi = sorted((r1, r2_))
    assert (
        confidence(ConfidenceInputs(d, lo, low, a)).value <= confidence(ConfidenceInputs(d, hi, low, a)).value
    )


@given(days, r2, age)
def test_flagged_fit_never_raises_confidence(d: float, r: float | None, a: timedelta | None) -> None:
    assert (
        confidence(ConfidenceInputs(d, r, True, a)).value
        <= confidence(ConfidenceInputs(d, r, False, a)).value
    )


@given(days, r2, st.booleans(), age, age)
def test_older_readings_never_raise_confidence(
    d: float, r: float | None, low: bool, a1: timedelta | None, a2: timedelta | None
) -> None:
    # None (no reading at all) counts as infinitely old.
    def key(x: timedelta | None) -> timedelta:
        return x if x is not None else timedelta.max

    fresh, stale = sorted((a1, a2), key=key)
    assert (
        confidence(ConfidenceInputs(d, r, low, stale)).value
        <= confidence(ConfidenceInputs(d, r, low, fresh)).value
    )
