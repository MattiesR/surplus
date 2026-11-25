"""Cost model: tokens → percent of a limit, per model family and bucket (plan §5.3). Pure, no I/O.

    pct ≈ Σ_m a_m · (w_in·in + w_out·out + w_cc·cache_create + w_cr·cache_read)_m

The token-kind weights `w_*` are fixed from the API price ratios (the prior, shared with `collector.split`).
The per-family coefficients `a_m` (percent per weighted token) are fitted with non-negative least squares
on windows between two consecutive authoritative readings of the same bucket that also contain log data.

Safety (plan §6.4): a fit with R² < 0.7, too few windows, or no usable signal is flagged `low_confidence`,
so callers widen their margins. Families without a positive fitted coefficient never cost "nothing":
they get the price-ratio prior scaled from the best-fitted family, which over-estimates rather than
under-estimates agent spend.

Note: the fitted percent also contains usage Claude Code cannot see (claude.ai chat etc., plan §5.2).
That noise lowers R² rather than biasing it silently; callers may pre-filter windows with invisible usage.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

import numpy as np
import numpy.typing as npt

from surplus.clock import Clock
from surplus.collector.split import (
    DEFAULT_MULTIPLIER,
    MODEL_MULTIPLIER,
    W_CACHE_CREATE,
    W_CACHE_READ,
    W_IN,
    W_OUT,
)
from surplus.domain.models import LimitReading, TokenCounts

R2_THRESHOLD = 0.7
MIN_WINDOWS = 8
# Readings further apart than this are not paired: a long gap hides resets and mixes unrelated usage.
MAX_WINDOW = timedelta(hours=6)
# Two readings belong to the same limit window if their reported resets agree within this tolerance.
RESET_TOLERANCE = timedelta(minutes=5)

OTHER_FAMILY = "other"
FAMILIES: tuple[str, ...] = (*MODEL_MULTIPLIER, OTHER_FAMILY)


def model_family(model: str) -> str:
    """Map a model id (`claude-opus-5-5`, …) to the family its coefficient is fitted for."""
    lowered = model.lower()
    for family in MODEL_MULTIPLIER:
        if family in lowered:
            return family
    return OTHER_FAMILY


def family_multiplier(family: str) -> float:
    return MODEL_MULTIPLIER.get(family, DEFAULT_MULTIPLIER)


@dataclass(frozen=True)
class TokenWeights:
    """Relative cost of each token kind (input = 1). Fixed from API price ratios, not fitted."""

    input: float = W_IN
    output: float = W_OUT
    cache_create: float = W_CACHE_CREATE
    cache_read: float = W_CACHE_READ

    def apply(self, t: TokenCounts) -> float:
        return (
            self.input * t.input
            + self.output * t.output
            + self.cache_create * t.cache_create
            + self.cache_read * t.cache_read
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_create": self.cache_create,
            "cache_read": self.cache_read,
        }


PRICE_RATIO_WEIGHTS = TokenWeights()


@dataclass(frozen=True)
class TokenEvent:
    """Token usage of one API response; the minimum the cost model needs from a usage event."""

    ts: datetime
    model: str
    tokens: TokenCounts


@dataclass(frozen=True)
class FitWindow:
    """One observation: how far a bucket moved between two readings, and the tokens spent in between."""

    bucket: str
    start: datetime
    end: datetime
    delta_pct: float
    tokens: Mapping[str, TokenCounts]  # by model family


class CoefficientSource(StrEnum):
    FIT = "fit"  # fitted by NNLS from this bucket's windows
    PRIOR = "prior"  # no usable data: price-ratio prior scaled from the reference family


@dataclass(frozen=True)
class ModelCoefficient:
    family: str
    pct_per_weighted_token: float
    source: CoefficientSource
    n_windows: int  # windows in which this family spent tokens


@dataclass(frozen=True)
class CostModelFit:
    bucket: str
    fitted_at: datetime
    weights: TokenWeights
    coefficients: Mapping[str, ModelCoefficient]  # every family in FAMILIES, or empty if nothing fitted
    r2: float | None  # None when there was nothing to fit
    n_windows: int
    low_confidence: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def coefficient(self, model: str) -> ModelCoefficient | None:
        return self.coefficients.get(model_family(model)) or self.coefficients.get(OTHER_FAMILY)


# --------------------------------------------------------------------------------------------- windows


def _sum_tokens(a: TokenCounts, b: TokenCounts) -> TokenCounts:
    return TokenCounts(
        input=a.input + b.input,
        output=a.output + b.output,
        cache_create=a.cache_create + b.cache_create,
        cache_read=a.cache_read + b.cache_read,
    )


def _same_window(a: LimitReading, b: LimitReading) -> bool:
    if a.resets_at is None or b.resets_at is None:
        return False  # reset unknown (manual reading): cannot rule out a reset in between
    return abs(a.resets_at - b.resets_at) <= RESET_TOLERANCE


def build_windows(
    bucket: str,
    readings: Iterable[LimitReading],
    events: Iterable[TokenEvent],
    max_window: timedelta = MAX_WINDOW,
) -> list[FitWindow]:
    """Pair consecutive readings of `bucket` into fit windows with the tokens spent in `(start, end]`.

    A pair is skipped if a reset may lie between the readings (reset times differ or are unknown), if the
    percent went down, if the readings are more than `max_window` apart, or if no tokens were logged.
    """
    by_ts: dict[datetime, LimitReading] = {}
    for r in readings:
        if r.bucket != bucket:
            continue
        # Several sources can report at the same instant; keep the highest (most conservative) reading.
        if r.ts not in by_ts or r.used_pct > by_ts[r.ts].used_pct:
            by_ts[r.ts] = r
    ordered = [by_ts[ts] for ts in sorted(by_ts)]
    if len(ordered) < 2:
        return []
    evs = sorted(events, key=lambda e: e.ts)
    windows: list[FitWindow] = []
    i = 0
    for prev, cur in itertools.pairwise(ordered):
        while i < len(evs) and evs[i].ts <= prev.ts:
            i += 1
        tokens: dict[str, TokenCounts] = {}
        j = i
        while j < len(evs) and evs[j].ts <= cur.ts:
            fam = model_family(evs[j].model)
            tokens[fam] = _sum_tokens(tokens.get(fam, TokenCounts()), evs[j].tokens)
            j += 1
        delta = cur.used_pct - prev.used_pct
        if (
            not _same_window(prev, cur)
            or delta < 0
            or cur.ts - prev.ts > max_window
            or not any(t.total > 0 for t in tokens.values())
        ):
            continue
        windows.append(FitWindow(bucket, prev.ts, cur.ts, delta, tokens))
    return windows


# ------------------------------------------------------------------------------------------------ NNLS


def nnls(
    a: npt.NDArray[np.float64], b: npt.NDArray[np.float64], max_iter: int | None = None
) -> npt.NDArray[np.float64]:
    """Non-negative least squares, min ||Ax - b||_2 s.t. x >= 0 (Lawson-Hanson active set)."""
    m, n = a.shape
    if b.shape != (m,):
        raise ValueError("shape mismatch between A and b")
    x = np.zeros(n)
    if n == 0 or m == 0:
        return x
    passive = np.zeros(n, dtype=bool)
    tol = 10 * np.finfo(float).eps * max(m, n) * max(1.0, float(np.abs(a).max(initial=0.0)))
    limit = max_iter if max_iter is not None else 3 * n + 30
    w = a.T @ (b - a @ x)
    for _ in range(limit):
        if passive.all() or not (w[~passive] > tol).any():
            break
        candidates = np.where(~passive, w, -np.inf)
        passive[int(np.argmax(candidates))] = True
        while True:
            z = np.zeros(n)
            idx = np.flatnonzero(passive)
            z[idx] = np.linalg.lstsq(a[:, idx], b, rcond=None)[0]
            if (z[idx] > tol).all():
                x = z
                break
            # Step back towards x until the first passive variable hits zero, then drop it.
            neg = passive & (z <= tol)
            alpha = float(np.min(x[neg] / (x[neg] - z[neg] + 1e-300)))
            x = x + alpha * (z - x)
            passive &= x > tol
            x[~passive] = 0.0
            if not passive.any():
                break
        w = a.T @ (b - a @ x)
    return x


def r_squared(y: npt.NDArray[np.float64], y_hat: npt.NDArray[np.float64]) -> float:
    """Coefficient of determination around the mean. A constant target is only "explained" if hit exactly."""
    ss_res = float(np.sum((y - y_hat) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2)) if y.size else 0.0
    if ss_tot <= 1e-12:
        return 1.0 if ss_res <= 1e-12 else 0.0
    return 1.0 - ss_res / ss_tot


# ------------------------------------------------------------------------------------------------- fit


def fit_cost_model(
    bucket: str,
    windows: Sequence[FitWindow],
    clock: Clock,
    weights: TokenWeights = PRICE_RATIO_WEIGHTS,
    min_windows: int = MIN_WINDOWS,
    r2_threshold: float = R2_THRESHOLD,
) -> CostModelFit:
    """Fit `a_m` for every model family on the windows of one bucket (other buckets are ignored)."""
    own = [w for w in windows if w.bucket == bucket]
    reasons: list[str] = []
    seen = [f for f in FAMILIES if any(w.tokens.get(f, TokenCounts()).total > 0 for w in own)]
    if len(own) < min_windows:
        reasons.append(f"only {len(own)} windows (need {min_windows})")
    if not seen:
        reasons.append("no windows with token data")
        return CostModelFit(bucket, clock.now(), weights, {}, None, len(own), True, tuple(reasons))

    x = np.array([[weights.apply(w.tokens.get(f, TokenCounts())) for f in seen] for w in own], dtype=float)
    y = np.array([w.delta_pct for w in own], dtype=float)
    # Columns are millions of weighted tokens; scale them to unit norm so the solver is well conditioned.
    scale = np.linalg.norm(x, axis=0)
    scale[scale == 0] = 1.0
    coef = nnls(x / scale, y) / scale
    r2 = r_squared(y, x @ coef)
    if r2 < r2_threshold:
        reasons.append(f"R² {r2:.3f} < {r2_threshold}")

    n_by_family = {f: sum(1 for w in own if w.tokens.get(f, TokenCounts()).total > 0) for f in FAMILIES}
    fitted = {f: float(c) for f, c in zip(seen, coef, strict=True) if c > 0 and math.isfinite(c)}
    if not fitted:
        reasons.append("no family has a positive coefficient")
        return CostModelFit(bucket, clock.now(), weights, {}, r2, len(own), True, tuple(reasons))

    # Families without a positive fit get the price-ratio prior, scaled from the best-supported family.
    ref = max(fitted, key=lambda f: (n_by_family[f], fitted[f]))
    per_unit = fitted[ref] / family_multiplier(ref)
    coefficients: dict[str, ModelCoefficient] = {}
    for f in FAMILIES:
        if f in fitted:
            coefficients[f] = ModelCoefficient(f, fitted[f], CoefficientSource.FIT, n_by_family[f])
        else:
            if n_by_family[f] > 0:
                reasons.append(f"{f}: fitted 0, using prior")
            coefficients[f] = ModelCoefficient(
                f, per_unit * family_multiplier(f), CoefficientSource.PRIOR, n_by_family[f]
            )
    return CostModelFit(
        bucket=bucket,
        fitted_at=clock.now(),
        weights=weights,
        coefficients=coefficients,
        r2=r2,
        n_windows=len(own),
        low_confidence=len(own) < min_windows or r2 < r2_threshold,
        reasons=tuple(reasons),
    )


def estimate_pct(fit: CostModelFit, usage: Iterable[tuple[str, TokenCounts]]) -> float | None:
    """Estimated percent of `fit.bucket` that `(model, tokens)` pairs cost; None without coefficients."""
    if not fit.coefficients:
        return None
    total = 0.0
    for model, tokens in usage:
        c = fit.coefficient(model)
        if c is None:
            return None
        total += c.pct_per_weighted_token * fit.weights.apply(tokens)
    return total
