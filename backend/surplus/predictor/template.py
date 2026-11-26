"""Working-hours template (plan §6.1 cold start, §16 Q3; ADR 0015) — pure.

Until the baseline has enough history it knows nothing about most slots and assumes the user could use the
whole window at any time. A template says *when* the user works. Inside working hours it stays just as
conservative:
- 5-hour bucket: the whole window per 5 hours (the no-data prior, 5 pp per 15-min slot);
- weekly bucket: the whole weekly limit spread over the working slots of a week.
Outside working hours the template is 0, so observed data (never lowered by the template) decides there.

Spec format: ``"mon-fri 09:00-18:00"``; several ranges separated by ``;``, e.g.
``"mon-fri 09:00-18:00; sat 10-13"``. Hours may omit minutes; times are local wall-clock time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from surplus.domain.models import FIVE_HOUR
from surplus.predictor.baseline import SLOTS_PER_DAY, SLOTS_PER_WEEK, no_data_pct

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RANGE = re.compile(
    r"^\s*(?P<d1>[a-z]{3})(?:\s*-\s*(?P<d2>[a-z]{3}))?\s+"
    r"(?P<h1>\d{1,2})(?::(?P<m1>\d{2}))?\s*-\s*(?P<h2>\d{1,2})(?::(?P<m2>\d{2}))?\s*$"
)


@dataclass(frozen=True)
class WorkingHours:
    spec: str
    busy: frozenset[int]  # hour-of-week slot indexes (Monday 00:00 = 0, local time)

    def describe(self) -> str:
        return f"{self.spec} ({len(self.busy) / 4:.0f} h a week)"


def _minutes(h: str, m: str | None) -> int:
    hours, mins = int(h), int(m or 0)
    if not (0 <= hours <= 24 and 0 <= mins < 60) or (hours == 24 and mins):
        raise ValueError(f"not a time: {h}:{m or '00'}")
    if mins % 15:
        raise ValueError("times must be on a quarter hour (e.g. 09:00, 17:45)")
    return hours * 60 + mins


def parse_working_hours(spec: str) -> WorkingHours:
    text = spec.strip().lower()
    if not text:
        raise ValueError("empty working hours")
    busy: set[int] = set()
    for part in text.split(";"):
        m = _RANGE.match(part)
        if not m:
            raise ValueError(f"not a range: {part.strip()!r} (use e.g. 'mon-fri 09:00-18:00')")
        d1, d2 = m.group("d1"), m.group("d2") or m.group("d1")
        if d1 not in DAYS or d2 not in DAYS:
            raise ValueError(f"unknown day in {part.strip()!r}; use {', '.join(DAYS)}")
        a, b = DAYS.index(d1), DAYS.index(d2)
        days = list(range(a, b + 1)) if a <= b else [*range(a, 7), *range(0, b + 1)]
        start, end = _minutes(m.group("h1"), m.group("m1")), _minutes(m.group("h2"), m.group("m2"))
        if end <= start:
            raise ValueError(f"the end must be after the start in {part.strip()!r}")
        for d in days:
            busy.update(d * SLOTS_PER_DAY + s for s in range(start // 15, end // 15))
    return WorkingHours(spec.strip(), frozenset(busy))


def template_for(hours: WorkingHours, bucket: str) -> list[float]:
    """P90 prior per hour-of-week slot, in percent of `bucket`'s limit."""
    if not hours.busy:
        return [0.0] * SLOTS_PER_WEEK
    busy_value = no_data_pct(FIVE_HOUR) if bucket == FIVE_HOUR else 100.0 / len(hours.busy)
    return [busy_value if i in hours.busy else 0.0 for i in range(SLOTS_PER_WEEK)]
