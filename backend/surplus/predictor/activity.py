"""Is the user working right now? (plan §6.3) — pure. Feeds the kill switch (§7.3).

The user counts as active when either
- a **manual Claude Code event** (a non-agent usage event from the host's logs or the status line) happened in
  the last `recent` minutes, or
- the 5-hour bucket **rose more than logged tokens explain** between two recent readings: usage Surplus cannot
  see (claude.ai chat, the desktop app, another machine). `explained_pct` is the cost-model estimate of
  everything logged in that interval (manual and agent), so an agent's own spend never looks like the user.

The thresholds err towards "active": a false positive only delays agent work; a false negative competes with
the user for their own limit.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from surplus.clock import ensure_utc

RECENT_MANUAL = timedelta(minutes=10)
RECENT_JUMP = timedelta(minutes=30)
JUMP_PCT = 1.0  # percentage points of the 5-hour window not explained by logged tokens


@dataclass(frozen=True)
class ReadingRise:
    """How far the 5-hour bucket moved between two consecutive readings in the same window."""

    start: datetime
    end: datetime
    rise_pct: float
    explained_pct: float | None  # None: no cost model, so nothing can be explained away

    @property
    def unexplained_pct(self) -> float:
        return self.rise_pct - (self.explained_pct or 0.0)


@dataclass(frozen=True)
class Activity:
    active: bool
    reason: str | None
    last_seen: datetime | None  # when the newest sign of activity happened


def user_activity(
    now: datetime,
    last_manual_event_at: datetime | None,
    rises: Iterable[ReadingRise],
    recent_manual: timedelta = RECENT_MANUAL,
    recent_jump: timedelta = RECENT_JUMP,
    jump_pct: float = JUMP_PCT,
) -> Activity:
    now = ensure_utc(now)
    signs: list[tuple[datetime, str]] = []
    if last_manual_event_at is not None:
        at = ensure_utc(last_manual_event_at)
        if now - at <= recent_manual:
            mins = max(0, round((now - at).total_seconds() / 60))
            signs.append((at, f"manual Claude Code use {mins} min ago"))
    for r in rises:
        end = ensure_utc(r.end)
        if now - end <= recent_jump and r.unexplained_pct >= jump_pct:
            signs.append(
                (end, f"5-hour usage rose {r.unexplained_pct:.1f} pp more than logged tokens explain")
            )
    if not signs:
        return Activity(False, None, None)
    at, reason = max(signs, key=lambda s: s[0])
    return Activity(True, reason, at)
