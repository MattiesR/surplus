"""Synthetic user profiles for the simulator (plan §13): demand in pp of the 5-hour window per 15-min slot.

Deterministic for a seed. Each profile is a generator over the whole horizon, so stateful behaviour
(sessions that last a while) is easy to express.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

SLOT = timedelta(minutes=15)


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    generate: Callable[[datetime, int, random.Random], list[float]]  # (start, slots, rng) -> pp5 per slot


def _light(start: datetime, slots: int, rng: random.Random) -> list[float]:
    out = []
    for i in range(slots):
        ts = start + i * SLOT
        busy = ts.weekday() < 5 and 9 <= ts.hour < 17 and rng.random() < 0.35
        out.append(rng.uniform(0.5, 1.5) if busy else 0.0)
    return out


def _heavy_weekday(start: datetime, slots: int, rng: random.Random) -> list[float]:
    out = []
    for i in range(slots):
        ts = start + i * SLOT
        busy = ts.weekday() < 5 and 8 <= ts.hour < 20 and rng.random() < 0.8
        out.append(rng.uniform(2.0, 5.0) if busy else 0.0)
    return out


def _irregular(start: datetime, slots: int, rng: random.Random) -> list[float]:
    """Evenings and weekends: one or two sessions a day, 1-3 h long, starting between 18:00 and 23:00
    on weekdays and any time from 09:00 on weekends."""
    out = [0.0] * slots
    day = 0
    while day * 96 < slots:
        day_start = start + timedelta(days=day)
        weekend = day_start.weekday() >= 5
        for _ in range(rng.choice((1, 1, 2))):
            hour = rng.uniform(9, 22) if weekend else rng.uniform(18, 23)
            first = day * 96 + int(hour * 4)
            for k in range(first, min(slots, first + rng.randint(4, 12))):
                out[k] += rng.uniform(1.0, 4.0)
        day += 1
    return out


PROFILES: dict[str, Profile] = {
    p.name: p
    for p in (
        Profile("light", "weekdays 09-17, short bursts, ~6% of the week", _light),
        Profile("heavy-weekday", "weekdays 08-20, nearly continuous, ~65% of the week", _heavy_weekday),
        Profile("irregular", "evenings and weekends in 1-3 h sessions, ~20% of the week", _irregular),
    )
}
