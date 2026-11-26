"""Seasonal quantile baseline (plan §6.1): P50/P90 of *manual* % per 15-min hour-of-week slot. Pure, no I/O.

Input is one observation per completed 15-min slot: how much of one bucket the user used by hand in it
(percent of that bucket's limit, plan §5.2: manual visible + invisible). Slots with no usage must be passed
as 0; slots that are missing are treated as "not observed" (Surplus was not running), not as "no usage".
`dense_observations` turns a sparse per-slot usage map plus a covered time range into that form.

Model (ADR 0010):
- **Hour-of-week**: 672 slots of 15 min, Monday 00:00 = 0, in the user's local time zone (habits follow the
  wall clock, also across DST changes).
- **Decay**: each observation weighs `0.5 ** (age / 3 weeks)`, so the model follows changing habits.
- **Neighbour smoothing**: a slot's distribution pools the observations of the slot itself and its
  neighbours (±1 slot, weight 0.25; the week wraps around). Pooling keeps P90 of a spike on its neighbours,
  so a user who starts at 9:00 one week and 9:15 the next is covered in both slots.
- **Quantiles**: weighted empirical quantiles (inverted CDF: the smallest observed value whose cumulative
  weight reaches q), so every P50/P90 is a value that actually happened.
- **Cold start** (< 2 weeks of observed slots, or a slot with fewer than 2 observations of its own): P90 is
  the highest manual usage seen in any *comparable* slot — the same time of day (± the neighbour radius) on
  any day — and never less than the user's "typical working hours" template, if one is given. A slot with
  no comparable observation uses the template, else the no-data prior: `100 / slots per bucket period`, i.e.
  "the user could use the whole window", which leaves nothing for agents until data arrives.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import StrEnum

from surplus.clock import Clock, ensure_utc
from surplus.domain.models import bucket_period_hours

SLOT = timedelta(minutes=15)
SLOTS_PER_DAY = 96
SLOTS_PER_WEEK = 7 * SLOTS_PER_DAY  # 672

HALF_LIFE = timedelta(weeks=3)
COLD_START_HISTORY = timedelta(weeks=2)
MIN_SLOT_OBSERVATIONS = 2
# Kernel weight of the slot itself, then of the slots ±1, ±2, … away.
# The neighbours together weigh less than the slot itself, so a consistent value keeps its own P50, but
# each neighbour's share (1/6) is above the 10% tail, so a spike still shows up in its neighbours' P90.
NEIGHBOUR_KERNEL: tuple[float, ...] = (1.0, 0.25)

P50, P90 = 0.5, 0.9


class SlotBasis(StrEnum):
    HISTORY = "history"  # weighted quantiles of this hour-of-week slot and its neighbours
    COLD_START = "cold_start"  # max of comparable time-of-day slots (and the template, if higher)
    TEMPLATE = "template"  # no comparable observation: the user's working-hours template
    NO_DATA = "no_data"  # nothing known: assume the whole window could be used


@dataclass(frozen=True)
class SlotObservation:
    """Manual usage of one bucket in one completed 15-min slot, in percent of the bucket's limit."""

    slot_start: datetime
    manual_pct: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.manual_pct) or self.manual_pct < 0:
            raise ValueError(f"manual_pct must be a finite value >= 0, got {self.manual_pct}")


@dataclass(frozen=True)
class BaselineConfig:
    half_life: timedelta = HALF_LIFE
    cold_start_history: timedelta = COLD_START_HISTORY
    min_slot_observations: int = MIN_SLOT_OBSERVATIONS
    kernel: tuple[float, ...] = NEIGHBOUR_KERNEL

    def __post_init__(self) -> None:
        if self.half_life <= timedelta(0):
            raise ValueError("half_life must be positive")
        if not self.kernel or self.kernel[0] <= 0 or any(k < 0 for k in self.kernel):
            raise ValueError("kernel needs a positive centre weight and non-negative neighbour weights")
        if self.min_slot_observations < 1:
            raise ValueError("min_slot_observations must be >= 1")


DEFAULT_CONFIG = BaselineConfig()


@dataclass(frozen=True)
class SlotForecast:
    slot_start: datetime
    bucket: str
    p50_pct: float
    p90_pct: float
    basis: SlotBasis


# ------------------------------------------------------------------------------------------- helpers


def slot_floor(ts: datetime) -> datetime:
    """Start of the 15-min slot containing `ts`, in UTC. Real UTC offsets are whole quarter hours, so UTC
    slot boundaries are local slot boundaries too."""
    t = ensure_utc(ts)
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


def hour_of_week(slot_start: datetime, tz: tzinfo = UTC) -> int:
    """Index 0..671 of the slot in local time: Monday 00:00 = 0, Sunday 23:45 = 671."""
    local = ensure_utc(slot_start).astimezone(tz)
    return local.weekday() * SLOTS_PER_DAY + local.hour * 4 + local.minute // 15


def decay_weight(age: timedelta, half_life: timedelta = HALF_LIFE) -> float:
    """Exponential decay: 1 now, 0.5 after one half-life. Future ages count as now."""
    return float(0.5 ** (max(age, timedelta(0)) / half_life))


def weighted_quantile(samples: Iterable[tuple[float, float]], q: float) -> float:
    """Weighted empirical quantile of `(value, weight)` pairs: the smallest value whose cumulative weight
    reaches `q` of the total (inverted CDF). Pairs with weight <= 0 are ignored."""
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0, 1], got {q}")
    pts = sorted((v, w) for v, w in samples if w > 0)
    if not pts:
        raise ValueError("weighted_quantile needs at least one sample with positive weight")
    total = math.fsum(w for _, w in pts)
    target = q * total * (1 - 1e-12)  # tolerate float error so q of a sum is reached exactly
    cum = 0.0
    for v, w in pts:
        cum += w
        if cum >= target:
            return v
    return pts[-1][0]


def no_data_pct(bucket: str) -> float:
    """No-data prior per slot: the whole bucket window spread evenly over its slots (sums to 100%)."""
    return 100.0 / (bucket_period_hours(bucket) * 4)


def dense_observations(
    usage: Mapping[datetime, float], start: datetime, end: datetime
) -> list[SlotObservation]:
    """One observation per slot in `[slot_floor(start), end)`; slots without usage are 0. Usage keyed by
    any timestamp is summed into its slot; usage outside the range is dropped."""
    first = slot_floor(start)
    stop = ensure_utc(end)
    by_slot: dict[datetime, float] = defaultdict(float)
    for ts, pct in usage.items():
        by_slot[slot_floor(ts)] += pct
    out: list[SlotObservation] = []
    cur = first
    while cur < stop:
        out.append(SlotObservation(cur, by_slot.get(cur, 0.0)))
        cur += SLOT
    return out


# ------------------------------------------------------------------------------------------------ model


@dataclass(frozen=True)
class SeasonalBaseline:
    """Fitted P50/P90 per hour-of-week slot for one bucket."""

    bucket: str
    fitted_at: datetime
    tz: tzinfo
    p50: tuple[float, ...]  # by hour-of-week index, len 672
    p90: tuple[float, ...]
    basis: tuple[SlotBasis, ...]
    history_days: float  # observed (completed, distinct) slots, in days
    cold_start: bool

    def slot(self, slot_start: datetime) -> SlotForecast:
        start = slot_floor(slot_start)
        i = hour_of_week(start, self.tz)
        return SlotForecast(start, self.bucket, self.p50[i], self.p90[i], self.basis[i])

    def forecast(self, start: datetime, end: datetime) -> list[SlotForecast]:
        """Forecasts for every slot overlapping `[start, end)`, starting with the one containing `start`."""
        out: list[SlotForecast] = []
        if ensure_utc(start) >= ensure_utc(end):
            return out
        cur, stop = slot_floor(start), ensure_utc(end)
        while cur < stop:
            out.append(self.slot(cur))
            cur += SLOT
        return out


def _pool(
    groups: Mapping[int, list[tuple[float, float]]], centre: int, period: int, kernel: Sequence[float]
) -> list[tuple[float, float]]:
    """Samples of `centre` and its neighbours on a circular index of length `period`, kernel-weighted."""
    pooled: list[tuple[float, float]] = []
    seen: set[int] = set()
    for dist, k in enumerate(kernel):
        if k <= 0:
            continue
        for idx in {(centre - dist) % period, (centre + dist) % period}:
            if idx in seen:
                continue
            seen.add(idx)
            pooled.extend((v, w * k) for v, w in groups.get(idx, ()))
    return pooled


def fit_baseline(
    bucket: str,
    observations: Iterable[SlotObservation],
    clock: Clock,
    tz: tzinfo = UTC,
    template: Sequence[float] | None = None,
    config: BaselineConfig = DEFAULT_CONFIG,
) -> SeasonalBaseline:
    """Fit the seasonal baseline for `bucket` at `clock.now()`.

    Only completed slots count (a slot still running would under-report). Several observations for the
    same slot are summed. `template`, if given, is a P90 prior per hour-of-week slot (len 672, % of this
    bucket); it is only used during cold start and can only raise the prior, never lower it below data.
    """
    now = clock.now()
    if template is not None:
        if len(template) != SLOTS_PER_WEEK:
            raise ValueError(f"template needs {SLOTS_PER_WEEK} slots, got {len(template)}")
        if any(not math.isfinite(t) or t < 0 for t in template):
            raise ValueError("template values must be finite and >= 0")

    by_slot: dict[datetime, float] = defaultdict(float)
    for o in observations:
        start = slot_floor(o.slot_start)
        if start + SLOT <= now:
            by_slot[start] += o.manual_pct

    how: dict[int, list[tuple[float, float]]] = defaultdict(list)  # hour-of-week → (value, decay weight)
    tod: dict[int, list[tuple[float, float]]] = defaultdict(list)  # time-of-day → (value, decay weight)
    for start, pct in by_slot.items():
        i = hour_of_week(start, tz)
        sample = (pct, decay_weight(now - start, config.half_life))
        how[i].append(sample)
        tod[i % SLOTS_PER_DAY].append(sample)

    history = SLOT * len(by_slot)
    cold_start = history < config.cold_start_history
    fallback = no_data_pct(bucket)

    p50: list[float] = []
    p90: list[float] = []
    basis: list[SlotBasis] = []
    for i in range(SLOTS_PER_WEEK):
        warm = not cold_start and len(how.get(i, ())) >= config.min_slot_observations
        if warm:
            pooled = _pool(how, i, SLOTS_PER_WEEK, config.kernel)
            hi = weighted_quantile(pooled, P90)
            mid = weighted_quantile(pooled, P50)
            b = SlotBasis.HISTORY
        else:
            pooled = _pool(tod, i % SLOTS_PER_DAY, SLOTS_PER_DAY, config.kernel)
            prior = template[i] if template is not None else None
            if pooled:
                hi = max(v for v, _ in pooled)
                if prior is not None:
                    hi = max(hi, prior)
                mid = weighted_quantile(pooled, P50)
                b = SlotBasis.COLD_START
            elif prior is not None:
                hi = mid = prior
                b = SlotBasis.TEMPLATE
            else:
                hi = mid = fallback
                b = SlotBasis.NO_DATA
        p90.append(hi)
        p50.append(min(mid, hi))
        basis.append(b)

    return SeasonalBaseline(
        bucket=bucket,
        fitted_at=now,
        tz=tz,
        p50=tuple(p50),
        p90=tuple(p90),
        basis=tuple(basis),
        history_days=history / timedelta(days=1),
        cold_start=cold_start,
    )
