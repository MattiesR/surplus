"""Global pause (plan §7.3): "stop using my subscription for agents for a while".

Stored in the `setting` table, so the CLI can set it while the server is down
and every process sees the same state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from surplus.clock import ensure_utc
from surplus.db.models import SettingRow

PAUSE_KEY = "pause"
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([mhd])\s*$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


@dataclass(frozen=True)
class PauseState:
    until: datetime | None  # None with `indefinite` = until resumed
    indefinite: bool
    reason: str
    since: datetime

    def active(self, now: datetime) -> bool:
        return self.indefinite or (self.until is not None and ensure_utc(now) < ensure_utc(self.until))

    def describe(self) -> str:
        when = "until you resume" if self.indefinite else f"until {self.until:%Y-%m-%d %H:%M} UTC"
        return f"paused {when}" + (f" ({self.reason})" if self.reason else "")


def parse_duration(text: str) -> timedelta:
    """'30m', '4h', '1.5h', '2d'."""
    m = _DURATION.match(text)
    if not m:
        raise ValueError(f"not a duration: {text!r} (use e.g. 30m, 4h or 2d)")
    span = timedelta(**{_UNITS[m.group(2)]: float(m.group(1))})
    if not timedelta(minutes=1) <= span <= timedelta(days=30):
        raise ValueError("a pause lasts between 1 minute and 30 days; use an indefinite pause otherwise")
    return span


def get_pause(session: Session, now: datetime) -> PauseState | None:
    """The pause in force at `now`, or None (an expired pause counts as none)."""
    row = session.get(SettingRow, PAUSE_KEY)
    if row is None:
        return None
    data = json.loads(row.value_json)
    state = PauseState(
        until=datetime.fromisoformat(data["until"]) if data.get("until") else None,
        indefinite=bool(data.get("indefinite")),
        reason=str(data.get("reason") or ""),
        since=datetime.fromisoformat(data["since"]),
    )
    return state if state.active(now) else None


def set_pause(session: Session, now: datetime, duration: timedelta | None, reason: str = "") -> PauseState:
    """Pause for `duration`, or indefinitely when it is None."""
    state = PauseState(
        until=ensure_utc(now) + duration if duration is not None else None,
        indefinite=duration is None,
        reason=reason.strip()[:200],
        since=ensure_utc(now),
    )
    payload = {
        "until": state.until.isoformat() if state.until else None,
        "indefinite": state.indefinite,
        "reason": state.reason,
        "since": state.since.isoformat(),
    }
    row = session.get(SettingRow, PAUSE_KEY)
    if row is None:
        session.add(SettingRow(key=PAUSE_KEY, value_json=json.dumps(payload)))
    else:
        row.value_json = json.dumps(payload)
    return state


def clear_pause(session: Session) -> bool:
    row = session.get(SettingRow, PAUSE_KEY)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True
