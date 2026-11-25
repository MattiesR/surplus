from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import FakeClock
from surplus.collector.sources.statusline import append_to_spool, ingest_spool, spool_line
from surplus.config import Paths
from surplus.db.models import LimitReadingRow

from conftest import T0

STATUS = {
    "cwd": "/home/me/secret-project",
    "transcript_path": "/home/me/.claude/projects/x.jsonl",
    "rate_limits": {
        "five_hour": {"used_percentage": 23.4, "resets_at": 1795653000},
        "seven_day": {"used_percentage": 41.0, "resets_at": 1796238000},
    },
}


def test_spool_line_keeps_only_rate_limits() -> None:
    line, text = spool_line(json.dumps(STATUS), T0)
    assert line is not None
    assert "secret-project" not in line and "transcript" not in line
    assert text == "5h 23% · 7d 41%"


def test_spool_line_tolerates_garbage() -> None:
    assert spool_line("not json", T0) == (None, "")
    assert spool_line(json.dumps({"model": "x"}), T0) == (None, "")


def test_ingest_spool_is_incremental(paths: Paths, sm: sessionmaker[Session], clock: FakeClock) -> None:
    spool = paths.statusline_spool
    line, _ = spool_line(json.dumps(STATUS), T0)
    assert line is not None
    append_to_spool(spool, line)
    assert ingest_spool(sm, spool, clock) == 2
    assert ingest_spool(sm, spool, clock) == 0
    with sm() as s:
        assert s.scalar(select(func.count()).select_from(LimitReadingRow)) == 2
