"""Contract tests on output recorded from the Claude CLI running inside a Podman container (spike 3).

The container authenticates with `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`). With that token the
`get_usage` probe reports no limits, so during runs the percentages come from the stream's `rate_limit_event`.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from surplus.collector.jsonl import parse_line
from surplus.collector.limits import parse_rate_limit_event

FIXTURES = Path(__file__).parent / "fixtures" / "runner"
TS = datetime(2026, 11, 25, 21, 0, tzinfo=UTC)


def _stream() -> list[dict[str, object]]:
    lines = (FIXTURES / "container_stream.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.startswith("{")]


def test_stream_rate_limit_event_gives_both_buckets() -> None:
    events = [e for e in _stream() if e.get("type") == "rate_limit_event"]
    assert len(events) == 1
    readings = {r.bucket: r for r in parse_rate_limit_event(events[0], TS)}
    assert set(readings) == {"five_hour", "seven_day"}
    assert readings["five_hour"].used_pct == 45.0
    assert readings["seven_day"].used_pct == 6.0
    assert readings["five_hour"].resets_at == datetime.fromtimestamp(1795653000, UTC)


def test_stream_result_reports_success() -> None:
    result = next(e for e in _stream() if e.get("type") == "result")
    assert result["subtype"] == "success"
    assert result["is_error"] is False


def test_container_session_log_is_ingestible() -> None:
    raw = (FIXTURES / "container_session.jsonl").read_text().splitlines()
    events = [e for line in raw if (e := parse_line(line)) is not None]
    assert len(events) == 1
    ev = events[0]
    assert ev.model == "claude-haiku-5-5"
    assert ev.tokens.input == 397
    assert ev.tokens.output == 4
