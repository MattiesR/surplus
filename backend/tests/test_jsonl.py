from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import FakeClock
from surplus.collector.jsonl import parse_line, read_new_lines
from surplus.collector.store import ingest_logs
from surplus.config import Paths
from surplus.db.models import UsageEventRow

from conftest import assistant_line, other_line


def _count(sm: sessionmaker[Session]) -> int:
    with sm() as s:
        return s.scalar(select(func.count()).select_from(UsageEventRow)) or 0


def test_parse_assistant_line() -> None:
    ev = parse_line(assistant_line("msg_1"))
    assert ev is not None
    assert ev.message_id == "msg_1"
    assert ev.model == "claude-opus-5-5"
    assert ev.tokens.output == 100
    assert ev.tokens.cache_read == 5000
    assert ev.ts.tzinfo is not None


def test_parse_skips_non_usage_lines() -> None:
    assert parse_line(other_line("user")) is None
    assert parse_line(other_line("summary")) is None
    assert parse_line("{not json") is None
    assert parse_line(json.dumps([1, 2])) is None
    assert parse_line(assistant_line("m", model="<synthetic>")) is None


def test_parse_falls_back_to_request_id() -> None:
    rec = json.loads(assistant_line("x"))
    del rec["message"]["id"]
    ev = parse_line(json.dumps(rec))
    assert ev is not None and ev.message_id == "req_x"


def test_bad_token_values_become_zero() -> None:
    ev = parse_line(
        assistant_line("m", usage={"input_tokens": -5, "output_tokens": "3", "cache_read_input_tokens": True})
    )
    assert ev is not None
    assert ev.tokens.total == 0


def test_partial_last_line_is_left_for_next_pass(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    full, partial = assistant_line("m1"), assistant_line("m2")
    f.write_text(full + "\n" + partial[:40])
    r1 = read_new_lines(f, None)
    assert [e.message_id for e in r1.events] == ["m1"]
    assert r1.cursor.offset == len(full) + 1
    with f.open("a") as fh:
        fh.write(partial[40:] + "\n")
    r2 = read_new_lines(f, r1.cursor)
    assert [e.message_id for e in r2.events] == ["m2"]


def test_truncated_file_is_reread_from_start(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text(assistant_line("m1") + "\n" + assistant_line("m2") + "\n")
    r1 = read_new_lines(f, None)
    f.write_text(assistant_line("m3") + "\n")
    r2 = read_new_lines(f, r1.cursor)
    assert [e.message_id for e in r2.events] == ["m3"]


def test_corrupt_line_is_counted_not_fatal(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('{"usage": broken\n' + assistant_line("m1") + "\n")
    r = read_new_lines(f, None)
    assert r.bad_lines == 1
    assert [e.message_id for e in r.events] == ["m1"]


def _write_session(paths: Paths, project: str, name: str, lines: list[str]) -> Path:
    d = paths.claude_dir / "projects" / project
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("".join(line + "\n" for line in lines))
    return f


def test_ingest_is_idempotent(paths: Paths, sm: sessionmaker[Session], clock: FakeClock) -> None:
    # One API response is logged as several lines (one per content block) with identical usage.
    _write_session(
        paths,
        "p",
        "a.jsonl",
        [assistant_line("m1"), assistant_line("m1"), other_line(), assistant_line("m2")],
    )
    s1 = ingest_logs(sm, paths.log_roots, clock)
    assert s1.events_inserted == 2
    s2 = ingest_logs(sm, paths.log_roots, clock)
    assert s2.events_inserted == 0
    assert s2.files_changed == 0
    assert _count(sm) == 2


def test_resumed_session_copies_are_not_double_counted(
    paths: Paths, sm: sessionmaker[Session], clock: FakeClock
) -> None:
    """ADR 0001: a resumed session copies history into a new file with a new session id."""
    _write_session(
        paths, "p", "a.jsonl", [assistant_line("m1", session="a"), assistant_line("m2", session="a")]
    )
    _write_session(
        paths,
        "p",
        "b.jsonl",
        [
            assistant_line("m1", session="b"),
            assistant_line("m2", session="b"),
            assistant_line("m3", session="b"),
        ],
    )
    ingest_logs(sm, paths.log_roots, clock)
    assert _count(sm) == 3


def test_ingest_appended_lines_only(paths: Paths, sm: sessionmaker[Session], clock: FakeClock) -> None:
    f = _write_session(paths, "p", "a.jsonl", [assistant_line("m1")])
    ingest_logs(sm, paths.log_roots, clock)
    with f.open("a") as fh:
        fh.write(assistant_line("m2") + "\n")
    s = ingest_logs(sm, paths.log_roots, clock)
    assert s.lines == 1
    assert s.events_inserted == 1


def test_ingest_scale(paths: Paths, sm: sessionmaker[Session], clock: FakeClock) -> None:
    """30 days of heavy use is ~5k responses; ingest 20k lines well under the 10 s target."""
    lines = []
    for i in range(20_000):
        lines.append(assistant_line(f"m{i // 2}", ts=f"2026-11-{1 + i % 25:02d}T10:00:00.000Z"))
        if i % 3 == 0:
            lines.append(other_line())
    _write_session(paths, "p", "big.jsonl", lines)
    s = ingest_logs(sm, paths.log_roots, clock)
    assert s.events_inserted == 10_000
    assert s.seconds < 10
