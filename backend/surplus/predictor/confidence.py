"""Forecast confidence in [0, 1] (plan §6.4) — pure.

Confidence scales the reserves of the budget (§7.1: `reserve * (2 - confidence)`), so low confidence means
bigger reserves and less agent work. It is the product of three factors, each in [0, 1]:

- **history**: how much observed usage the seasonal baseline has (full after 4 weeks, 0 without any);
- **cost model**: how well tokens explain the reported percent (R² of the fit, capped when the fit is flagged
  `low_confidence`, and a neutral 0.5 when there is no fit at all);
- **freshness**: how old the newest authoritative reading is (full up to 15 min, 0 from 2 h on).

A product, not an average: any one input being unknown makes the whole forecast untrustworthy. Each factor is
monotonic in its input, so the product is monotonic in every input (tested with hypothesis).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

FULL_HISTORY_DAYS = 28.0  # four weeks: the backtest horizon of plan §6.2
NO_FIT_FACTOR = 0.5  # no cost model yet: neither trusted nor ignored
LOW_CONFIDENCE_CAP = 0.6  # a flagged fit (R² < 0.7 or too few windows) never counts as good
R2_FLOOR, R2_FULL = 0.5, 0.9  # R² ≤ 0.5 → 0.5 of the factor's range; ≥ 0.9 → 1
FRESH_FOR = timedelta(minutes=15)
STALE_AFTER = timedelta(hours=2)


@dataclass(frozen=True)
class ConfidenceInputs:
    history_days: float  # SeasonalBaseline.history_days
    r_squared: float | None  # cost-model R² for the bucket; None without a fit
    cost_low_confidence: bool  # CostModelFit.low_confidence
    reading_age: timedelta | None  # age of the newest probe/stream reading; None if there is none


@dataclass(frozen=True)
class Confidence:
    value: float
    history: float
    cost_model: float
    freshness: float
    reasons: tuple[str, ...]  # human-readable, for the decision log and the UI


def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def history_factor(history_days: float) -> float:
    return _clamp(history_days / FULL_HISTORY_DAYS)


def cost_model_factor(r_squared: float | None, low_confidence: bool) -> float:
    if r_squared is None:
        return NO_FIT_FACTOR
    base = 0.5 + 0.5 * _clamp((r_squared - R2_FLOOR) / (R2_FULL - R2_FLOOR))
    return min(base, LOW_CONFIDENCE_CAP) if low_confidence else base


def freshness_factor(reading_age: timedelta | None) -> float:
    if reading_age is None:
        return 0.0
    age = max(reading_age, timedelta(0))
    if age <= FRESH_FOR:
        return 1.0
    return _clamp(1 - (age - FRESH_FOR) / (STALE_AFTER - FRESH_FOR))


def confidence(inputs: ConfidenceInputs) -> Confidence:
    h = history_factor(inputs.history_days)
    c = cost_model_factor(inputs.r_squared, inputs.cost_low_confidence)
    f = freshness_factor(inputs.reading_age)
    reasons: list[str] = []
    if h < 1:
        reasons.append(f"{inputs.history_days:.1f} of {FULL_HISTORY_DAYS:.0f} days of usage history")
    if inputs.r_squared is None:
        reasons.append("no cost-model fit yet")
    elif inputs.cost_low_confidence or c < 1:
        flag = " (flagged low confidence)" if inputs.cost_low_confidence else ""
        reasons.append(f"cost model R² {inputs.r_squared:.2f}{flag}")
    if inputs.reading_age is None:
        reasons.append("no authoritative reading")
    elif f < 1:
        reasons.append(f"newest reading is {inputs.reading_age.total_seconds() / 60:.0f} min old")
    return Confidence(round(h * c * f, 4), h, c, f, tuple(reasons))
