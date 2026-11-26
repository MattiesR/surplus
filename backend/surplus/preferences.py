"""User preferences in the `setting` table (plan §16), edited from the dashboard, the API or the CLI."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from surplus.db.models import SettingRow
from surplus.predictor.template import WorkingHours, parse_working_hours

WORKING_HOURS_KEY = "working_hours"


def get_working_hours(session: Session) -> WorkingHours | None:
    row = session.get(SettingRow, WORKING_HOURS_KEY)
    if row is None:
        return None
    spec = json.loads(row.value_json)
    try:
        return parse_working_hours(spec) if spec else None
    except ValueError:
        return None  # an old or hand-edited value that no longer parses: no template (the safe side)


def set_working_hours(session: Session, spec: str | None) -> WorkingHours | None:
    """Validate and store; None or "" removes the template."""
    hours = parse_working_hours(spec) if spec and spec.strip() else None
    row = session.get(SettingRow, WORKING_HOURS_KEY)
    if hours is None:
        if row is not None:
            session.delete(row)
            session.flush()
        return None
    value = json.dumps(hours.spec)
    if row is None:
        session.add(SettingRow(key=WORKING_HOURS_KEY, value_json=value))
    else:
        row.value_json = value
    return hours
