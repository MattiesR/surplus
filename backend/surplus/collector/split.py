"""Manual / agent / invisible split (plan §5.2) — pure functions.

Phase 1 uses the authoritative weekly breakdown from the probe (spike 2):
- `claude_code` share of the weekly limit = everything visible in Claude Code logs (manual + agent),
- every other surface (chat, cowork, other) = invisible.
Within the Claude Code share, agent vs manual is split by price-weighted tokens. The full fitted cost model
(tokens → %, plan §5.3) arrives in Phase 4; until then token weights are the API price-ratio prior.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from surplus.domain.models import SurfaceShare, TokenCounts

# Relative price weights per token kind (input = 1), the prior from plan §5.3.
W_IN, W_OUT, W_CACHE_CREATE, W_CACHE_READ = 1.0, 5.0, 1.25, 0.1

# Relative model cost (Haiku = 1). Unknown families are treated as the most expensive tier, which
# over-estimates visible spend and therefore keeps budgets conservative.
MODEL_MULTIPLIER = {"haiku": 1.0, "sonnet": 3.0, "opus": 5.0}
DEFAULT_MULTIPLIER = 5.0

VISIBLE_SURFACES = frozenset({"claude_code"})


def model_multiplier(model: str) -> float:
    lowered = model.lower()
    for family, mult in MODEL_MULTIPLIER.items():
        if family in lowered:
            return mult
    return DEFAULT_MULTIPLIER


def weighted_tokens(model: str, t: TokenCounts) -> float:
    raw = W_IN * t.input + W_OUT * t.output + W_CACHE_CREATE * t.cache_create + W_CACHE_READ * t.cache_read
    return raw * model_multiplier(model)


@dataclass(frozen=True)
class Split:
    manual: float
    agent: float
    invisible: float

    @property
    def total(self) -> float:
        return self.manual + self.agent + self.invisible


def weekly_split(breakdown: Sequence[SurfaceShare], manual_wt: float, agent_wt: float) -> Split | None:
    """Split the weekly limit used into manual (visible), agent and invisible, in percent of the limit."""
    if not breakdown:
        return None
    visible = sum(s.pct_of_limit for s in breakdown if s.key in VISIBLE_SURFACES)
    invisible = sum(s.pct_of_limit for s in breakdown if s.key not in VISIBLE_SURFACES)
    total_wt = manual_wt + agent_wt
    agent_share = agent_wt / total_wt if total_wt > 0 else 0.0
    return Split(manual=visible * (1 - agent_share), agent=visible * agent_share, invisible=invisible)


@dataclass(frozen=True)
class WeightedEvent:
    ts: datetime
    weight: float
    is_agent: bool


@dataclass(frozen=True)
class SurfacePoint:
    ts: datetime
    invisible_pct: float  # cumulative invisible share of the weekly limit at `ts`


@dataclass(frozen=True)
class SeriesPoint:
    start: datetime
    manual: float
    agent: float
    invisible: float


def hourly_series(
    events: Iterable[WeightedEvent],
    surfaces: Sequence[SurfacePoint],
    visible_pct: float,
    start: datetime,
    end: datetime,
    step: timedelta = timedelta(hours=1),
) -> list[SeriesPoint]:
    """Per-slot usage in % of the weekly limit, between `start` and `end`.

    Visible usage is scaled so the slots sum to `visible_pct` (the authoritative claude_code share).
    Invisible usage is the increase of the cumulative invisible share between consecutive surface readings,
    attributed to the slot of the later reading (clamped at >= 0; a weekly reset drops it to 0).
    """
    n = max(0, int((end - start) / step))
    manual = [0.0] * n
    agent = [0.0] * n
    invisible = [0.0] * n
    total_wt = 0.0
    for e in events:
        idx = int((e.ts - start) / step)
        if 0 <= idx < n:
            (agent if e.is_agent else manual)[idx] += e.weight
            total_wt += e.weight
    scale = visible_pct / total_wt if total_wt > 0 else 0.0
    ordered = sorted(surfaces, key=lambda p: p.ts)
    for prev, cur in itertools.pairwise(ordered):
        idx = int((cur.ts - start) / step)
        if 0 <= idx < n:
            invisible[idx] += max(0.0, cur.invisible_pct - prev.invisible_pct)
    return [
        SeriesPoint(start + i * step, manual[i] * scale, agent[i] * scale, invisible[i]) for i in range(n)
    ]
