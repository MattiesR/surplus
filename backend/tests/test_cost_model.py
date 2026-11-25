"""Cost model (plan §5.3): NNLS, window building, synthetic recovery of known coefficients, R² flagging."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from surplus.clock import FakeClock
from surplus.domain.models import FIVE_HOUR, SEVEN_DAY, LimitReading, ReadingSource, TokenCounts
from surplus.predictor.cost_model import (
    FAMILIES,
    PRICE_RATIO_WEIGHTS,
    R2_THRESHOLD,
    CoefficientSource,
    FitWindow,
    TokenEvent,
    build_windows,
    estimate_pct,
    fit_cost_model,
    model_family,
    nnls,
    r_squared,
)

from conftest import T0

TRUE = {"haiku": 2e-6, "sonnet": 6e-6, "opus": 1e-5}
RESET = T0 + timedelta(hours=3)


def _brute_force_nnls(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Exact NNLS by enumerating every support set (fine for a handful of columns)."""
    n = a.shape[1]
    best, best_res = np.zeros(n), float(np.sum(b**2))
    for k in range(1, n + 1):
        for support in itertools.combinations(range(n), k):
            idx = list(support)
            sol = np.linalg.lstsq(a[:, idx], b, rcond=None)[0]
            if (sol < 0).any():
                continue
            x = np.zeros(n)
            x[idx] = sol
            res = float(np.sum((a @ x - b) ** 2))
            if res < best_res - 1e-12:
                best, best_res = x, res
    return best


def _tokens(rng: np.random.Generator) -> TokenCounts:
    return TokenCounts(
        input=int(rng.integers(0, 20_000)),
        output=int(rng.integers(0, 40_000)),
        cache_create=int(rng.integers(0, 100_000)),
        cache_read=int(rng.integers(0, 2_000_000)),
    )


def _synthetic_windows(
    n: int, coef: dict[str, float], noise_sd: float = 0.0, seed: int = 1, bucket: str = FIVE_HOUR
) -> list[FitWindow]:
    rng = np.random.default_rng(seed)
    out = []
    for k in range(n):
        tokens = {f: _tokens(rng) for f in coef if rng.random() < 0.7}
        if not tokens:
            tokens = {"opus": _tokens(rng)}
        pct = sum(coef[f] * PRICE_RATIO_WEIGHTS.apply(t) for f, t in tokens.items())
        pct = max(0.0, pct + rng.normal(0, noise_sd))
        start = T0 + timedelta(minutes=15 * k)
        out.append(FitWindow(bucket, start, start + timedelta(minutes=15), pct, tokens))
    return out


def _reading(
    ts: datetime, pct: float, resets_at: datetime | None = RESET, bucket: str = FIVE_HOUR
) -> LimitReading:
    return LimitReading(ts=ts, bucket=bucket, used_pct=pct, resets_at=resets_at, source=ReadingSource.PROBE)


# ------------------------------------------------------------------------------------------------ NNLS


def test_nnls_matches_least_squares_when_unconstrained_solution_is_positive() -> None:
    rng = np.random.default_rng(0)
    a = rng.random((30, 3))
    x_true = np.array([1.0, 2.0, 3.0])
    np.testing.assert_allclose(nnls(a, a @ x_true), x_true, rtol=1e-9)


def test_nnls_clamps_negative_coefficients() -> None:
    a = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    b = np.array([2.0, -1.0, 1.0])
    x = nnls(a, b)
    assert (x >= 0).all()
    np.testing.assert_allclose(x, _brute_force_nnls(a, b), atol=1e-9)


@pytest.mark.parametrize("seed", range(25))
def test_nnls_agrees_with_brute_force(seed: int) -> None:
    rng = np.random.default_rng(seed)
    m, n = int(rng.integers(3, 12)), int(rng.integers(1, 5))
    a = rng.normal(size=(m, n))
    b = rng.normal(size=m)
    x = nnls(a, b)
    assert (x >= 0).all()
    exact = _brute_force_nnls(a, b)
    assert np.sum((a @ x - b) ** 2) == pytest.approx(np.sum((a @ exact - b) ** 2), rel=1e-7, abs=1e-9)


def test_nnls_empty_and_shape_checks() -> None:
    assert nnls(np.zeros((0, 2)), np.zeros(0)).tolist() == [0.0, 0.0]
    with pytest.raises(ValueError, match="shape"):
        nnls(np.zeros((3, 2)), np.zeros(2))


def test_r_squared() -> None:
    y = np.array([1.0, 2.0, 3.0])
    assert r_squared(y, y) == 1.0
    assert r_squared(y, np.full(3, 2.0)) == 0.0
    assert r_squared(np.ones(3), np.ones(3)) == 1.0
    assert r_squared(np.ones(3), np.zeros(3)) == 0.0


def test_model_family() -> None:
    assert model_family("claude-opus-5-5") == "opus"
    assert model_family("claude-Sonnet-5-5") == "sonnet"
    assert model_family("claude-haiku-5-5") == "haiku"
    assert model_family("claude-fable-5-2") == "other"


# ------------------------------------------------------------------------------------- synthetic fits


def test_recovers_known_coefficients_without_noise(clock: FakeClock) -> None:
    fit = fit_cost_model(FIVE_HOUR, _synthetic_windows(40, TRUE), clock)
    assert not fit.low_confidence
    assert fit.r2 == pytest.approx(1.0)
    assert fit.n_windows == 40
    assert fit.fitted_at == T0
    for f, a in TRUE.items():
        c = fit.coefficients[f]
        assert c.source is CoefficientSource.FIT
        assert c.pct_per_weighted_token == pytest.approx(a, rel=1e-6)
        assert c.n_windows > 0
    assert set(fit.coefficients) == set(FAMILIES)


def test_recovers_known_coefficients_with_noise(clock: FakeClock) -> None:
    fit = fit_cost_model(FIVE_HOUR, _synthetic_windows(300, TRUE, noise_sd=0.1, seed=7), clock)
    assert not fit.low_confidence
    assert fit.r2 is not None
    assert fit.r2 > 0.95
    for f, a in TRUE.items():
        assert fit.coefficients[f].pct_per_weighted_token == pytest.approx(a, rel=0.05)


def test_unseen_family_gets_price_ratio_prior(clock: FakeClock) -> None:
    fit = fit_cost_model(FIVE_HOUR, _synthetic_windows(40, {"opus": 1e-7}), clock)
    assert fit.coefficients["opus"].source is CoefficientSource.FIT
    for f, mult in (("haiku", 1.0), ("sonnet", 3.0), ("other", 5.0)):
        c = fit.coefficients[f]
        assert c.source is CoefficientSource.PRIOR
        assert c.n_windows == 0
        assert c.pct_per_weighted_token == pytest.approx(1e-7 / 5.0 * mult, rel=1e-6)


def test_family_fitted_to_zero_falls_back_to_prior(clock: FakeClock) -> None:
    # The bucket "punishes" sonnet tokens: unconstrained LS would give sonnet a negative cost.
    rng = np.random.default_rng(3)
    windows = []
    for k in range(30):
        opus, sonnet = _tokens(rng), _tokens(rng)
        pct = max(0.0, 1e-7 * PRICE_RATIO_WEIGHTS.apply(opus) - 1e-8 * PRICE_RATIO_WEIGHTS.apply(sonnet))
        start = T0 + timedelta(hours=k)
        windows.append(FitWindow(FIVE_HOUR, start, start, pct, {"opus": opus, "sonnet": sonnet}))
    fit = fit_cost_model(FIVE_HOUR, windows, clock)
    sonnet = fit.coefficients["sonnet"]
    assert sonnet.source is CoefficientSource.PRIOR
    assert sonnet.pct_per_weighted_token > 0  # never "free"
    assert any("sonnet" in r for r in fit.reasons)


def test_low_r2_is_flagged(clock: FakeClock) -> None:
    rng = np.random.default_rng(11)
    windows = _synthetic_windows(60, TRUE, seed=11)
    noisy = [FitWindow(w.bucket, w.start, w.end, float(rng.uniform(0, 10)), w.tokens) for w in windows]
    fit = fit_cost_model(FIVE_HOUR, noisy, clock)
    assert fit.r2 is not None
    assert fit.r2 < R2_THRESHOLD
    assert fit.low_confidence
    assert any("R²" in r for r in fit.reasons)


def test_good_r2_with_too_few_windows_is_flagged(clock: FakeClock) -> None:
    fit = fit_cost_model(FIVE_HOUR, _synthetic_windows(5, TRUE), clock)
    assert fit.r2 == pytest.approx(1.0)
    assert fit.low_confidence
    assert any("only 5 windows" in r for r in fit.reasons)


def test_no_data_gives_empty_flagged_fit(clock: FakeClock) -> None:
    fit = fit_cost_model(FIVE_HOUR, [], clock)
    assert fit.coefficients == {}
    assert fit.r2 is None
    assert fit.low_confidence
    assert estimate_pct(fit, [("claude-opus-5-5", TokenCounts(output=10))]) is None


def test_fit_ignores_other_buckets(clock: FakeClock) -> None:
    windows = _synthetic_windows(20, TRUE) + _synthetic_windows(20, {"opus": 5e-7}, bucket=SEVEN_DAY)
    fit = fit_cost_model(FIVE_HOUR, windows, clock)
    assert fit.n_windows == 20
    assert fit.coefficients["opus"].pct_per_weighted_token == pytest.approx(TRUE["opus"], rel=1e-6)


def test_estimate_pct(clock: FakeClock) -> None:
    fit = fit_cost_model(FIVE_HOUR, _synthetic_windows(40, TRUE), clock)
    t = TokenCounts(input=1000, output=2000, cache_create=0, cache_read=10_000)
    wt = PRICE_RATIO_WEIGHTS.apply(t)  # 1000 + 10000 + 1000
    assert wt == pytest.approx(12_000)
    est = estimate_pct(fit, [("claude-opus-5-5", t), ("claude-haiku-5-5", t)])
    assert est == pytest.approx((TRUE["opus"] + TRUE["haiku"]) * wt, rel=1e-6)
    # Unknown families use the conservative "other" coefficient (priced like the top tier).
    assert estimate_pct(fit, [("claude-fable-5-2", t)]) == pytest.approx(TRUE["opus"] * wt, rel=1e-6)


# ------------------------------------------------------------------------------------------- windows


def test_build_windows_pairs_readings_and_sums_tokens_per_family() -> None:
    t1, t2, t3 = T0, T0 + timedelta(minutes=15), T0 + timedelta(minutes=30)
    readings = [_reading(t1, 10), _reading(t2, 12), _reading(t3, 12), _reading(t2, 50, bucket=SEVEN_DAY)]
    events = [
        TokenEvent(t1, "claude-opus-5-5", TokenCounts(output=999)),  # at start: belongs to the window before
        TokenEvent(t1 + timedelta(minutes=1), "claude-opus-5-5", TokenCounts(output=10)),
        TokenEvent(t1 + timedelta(minutes=2), "claude-opus-5-5", TokenCounts(input=5)),
        TokenEvent(t2, "claude-sonnet-5-5", TokenCounts(cache_read=7)),  # at end: included
        TokenEvent(t2 + timedelta(minutes=5), "claude-haiku-5-5", TokenCounts(input=1)),
    ]
    windows = build_windows(FIVE_HOUR, readings, events)
    assert len(windows) == 2
    w1, w2 = windows
    assert (w1.start, w1.end, w1.delta_pct) == (t1, t2, 2)
    assert w1.tokens == {"opus": TokenCounts(input=5, output=10), "sonnet": TokenCounts(cache_read=7)}
    assert (w2.start, w2.end, w2.delta_pct) == (t2, t3, 0)
    assert w2.tokens == {"haiku": TokenCounts(input=1)}


def test_build_windows_skips_unsafe_pairs() -> None:
    ev = [
        TokenEvent(T0 + timedelta(hours=h, minutes=1), "claude-opus-5-5", TokenCounts(output=1))
        for h in range(12)
    ]
    readings = [
        _reading(T0, 10),
        _reading(T0 + timedelta(hours=1), 5),  # percent went down
        _reading(T0 + timedelta(hours=2), 1, resets_at=RESET + timedelta(hours=5)),  # reset in between
        _reading(T0 + timedelta(hours=3), 2, resets_at=None),  # manual: reset unknown
        _reading(T0 + timedelta(hours=4), 3, resets_at=RESET + timedelta(hours=5)),  # after unknown
        _reading(T0 + timedelta(hours=11), 9, resets_at=RESET + timedelta(hours=5, minutes=3)),  # gap > 6h
    ]
    assert build_windows(FIVE_HOUR, readings, ev) == []


def test_build_windows_tolerates_small_reset_jitter_and_needs_tokens() -> None:
    readings = [
        _reading(T0, 10),
        _reading(T0 + timedelta(minutes=15), 11, resets_at=RESET + timedelta(minutes=2)),
        _reading(T0 + timedelta(minutes=30), 11),
    ]
    ev = [TokenEvent(T0 + timedelta(minutes=5), "claude-opus-5-5", TokenCounts(output=1))]
    windows = build_windows(FIVE_HOUR, readings, ev)
    assert [(w.start, w.end) for w in windows] == [(T0, T0 + timedelta(minutes=15))]


def test_build_windows_keeps_highest_reading_per_instant() -> None:
    readings = [
        _reading(T0, 10),
        _reading(T0 + timedelta(minutes=15), 12),
        LimitReading(
            ts=T0 + timedelta(minutes=15),
            bucket=FIVE_HOUR,
            used_pct=13,
            resets_at=RESET,
            source=ReadingSource.STREAM,
        ),
    ]
    ev = [TokenEvent(T0 + timedelta(minutes=5), "claude-opus-5-5", TokenCounts(output=1))]
    (w,) = build_windows(FIVE_HOUR, readings, ev)
    assert w.delta_pct == 3


def test_end_to_end_recovery_from_readings_and_events() -> None:
    """Simulate two days of 15-minute probe readings with known costs; the fit recovers them."""
    rng = np.random.default_rng(5)
    clock = FakeClock(datetime(2026, 11, 1, tzinfo=UTC))
    start = clock.now()
    readings: list[LimitReading] = []
    events: list[TokenEvent] = []
    used, reset = 0.0, start + timedelta(hours=5)
    models = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
    for k in range(4 * 48):
        ts = start + timedelta(minutes=15 * k)
        if ts >= reset:
            used, reset = 0.0, reset + timedelta(hours=5)
        readings.append(_reading(ts, round(used, 6), resets_at=reset))
        for _ in range(int(rng.integers(0, 4))):
            fam = str(rng.choice(list(models)))
            t = TokenCounts(
                input=int(rng.integers(0, 3000)),
                output=int(rng.integers(0, 8000)),
                cache_create=int(rng.integers(0, 20_000)),
                cache_read=int(rng.integers(0, 300_000)),
            )
            events.append(TokenEvent(ts + timedelta(minutes=int(rng.integers(1, 15))), models[fam], t))
            used = min(100.0, used + TRUE[fam] * PRICE_RATIO_WEIGHTS.apply(t))
    clock.advance(timedelta(days=2))
    windows = build_windows(FIVE_HOUR, readings, events)
    assert len(windows) > 100
    fit = fit_cost_model(FIVE_HOUR, windows, clock)
    assert not fit.low_confidence
    for f, a in TRUE.items():
        assert fit.coefficients[f].pct_per_weighted_token == pytest.approx(a, rel=1e-3)
