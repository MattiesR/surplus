"""Per-slot *user* usage of one bucket, from readings and logged tokens (baseline input, §6.1) — pure.

"User" = everything that is not a Surplus agent: manual Claude Code use (logged) plus usage Surplus cannot see
(claude.ai, the desktop app, other machines). Both compete with agents for the same limit (ADR 0011).

For every pair of consecutive readings in the same window (same reported reset, at most `max_gap` apart, not
going down), the interval `(a, b]` is *covered*:
- each manual event adds its estimated percent (cost model) to the slot it falls in;
- the rise the logs do not explain (`rise - manual - agent`, if positive) is invisible user usage, spread over
  the interval's slots in proportion to their overlap.
Without a cost model nothing can be explained, so the whole rise counts as user usage (agent spend included):
over-estimating the user is the safe direction.

A completed slot is *observed* when at least half of it is covered; it is then reported, with 0 if nothing
happened. Uncovered slots are left out: "not observed" is different from "no usage" (ADR 0010).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from surplus.clock import ensure_utc
from surplus.domain.models import LimitReading
from surplus.predictor.baseline import SLOT, SlotObservation, slot_floor

MAX_GAP = timedelta(hours=6)
RESET_TOLERANCE = timedelta(minutes=5)
MIN_COVERAGE = 0.5


@dataclass(frozen=True)
class CostedEvent:
    ts: datetime
    pct: float | None  # estimated % of the bucket (cost model); None when there is no coefficient
    is_agent: bool


def _pairs(
    bucket: str, readings: Iterable[LimitReading], max_gap: timedelta
) -> list[tuple[LimitReading, LimitReading]]:
    by_ts: dict[datetime, LimitReading] = {}
    for r in readings:
        if r.bucket != bucket or r.resets_at is None:
            continue  # manual readings carry no reset: a reset could hide between them
        ts = ensure_utc(r.ts)
        if ts not in by_ts or r.used_pct > by_ts[ts].used_pct:
            by_ts[ts] = r
    ordered = [by_ts[t] for t in sorted(by_ts)]
    out = []
    for a, b in itertools.pairwise(ordered):
        ra, rb = a.resets_at, b.resets_at
        if (
            ra is not None
            and rb is not None
            and abs(ensure_utc(ra) - ensure_utc(rb)) <= RESET_TOLERANCE
            and ensure_utc(b.ts) - ensure_utc(a.ts) <= max_gap
            and b.used_pct >= a.used_pct
        ):
            out.append((a, b))
    return out


def _overlaps(start: datetime, end: datetime) -> list[tuple[datetime, float]]:
    """(slot_start, seconds of `(start, end]` inside that slot)."""
    out = []
    cur = slot_floor(start)
    while cur < end:
        lo, hi = max(cur, start), min(cur + SLOT, end)
        if hi > lo:
            out.append((cur, (hi - lo).total_seconds()))
        cur += SLOT
    return out


def user_slot_observations(
    bucket: str,
    readings: Iterable[LimitReading],
    events: Iterable[CostedEvent],
    now: datetime,
    max_gap: timedelta = MAX_GAP,
) -> list[SlotObservation]:
    now = ensure_utc(now)
    evs = sorted(events, key=lambda e: e.ts)
    usage: dict[datetime, float] = {}
    covered: dict[datetime, float] = {}
    i = 0
    for a, b in _pairs(bucket, readings, max_gap):
        start, end = ensure_utc(a.ts), ensure_utc(b.ts)
        while i < len(evs) and ensure_utc(evs[i].ts) <= start:
            i += 1
        inside = []
        j = i
        while j < len(evs) and ensure_utc(evs[j].ts) <= end:
            inside.append(evs[j])
            j += 1
        rise = b.used_pct - a.used_pct
        overlaps = _overlaps(start, end)
        span = (end - start).total_seconds()
        known = all(e.pct is not None for e in inside)
        if known:
            explained = sum(e.pct or 0.0 for e in inside)
            for e in inside:
                if not e.is_agent:
                    s = slot_floor(e.ts)
                    usage[s] = usage.get(s, 0.0) + (e.pct or 0.0)
            invisible = max(0.0, rise - explained)
        else:
            invisible = rise  # nothing can be explained away: all of it may be the user
        for s, secs in overlaps:
            covered[s] = covered.get(s, 0.0) + secs
            usage[s] = usage.get(s, 0.0) + invisible * secs / span
    out = []
    for s in sorted(covered):
        if s + SLOT <= now and covered[s] >= MIN_COVERAGE * SLOT.total_seconds():
            out.append(SlotObservation(s, max(0.0, usage.get(s, 0.0))))
    return out
