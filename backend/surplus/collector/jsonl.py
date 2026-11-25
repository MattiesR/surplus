"""Claude Code JSONL log parsing (spike 1) and incremental tailing.

`parse_line` is pure. `read_new_lines` does the file I/O: it reads complete lines from a byte offset and
never consumes a trailing partial line, so a log that is being written is picked up on the next pass.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from surplus.clock import ensure_utc
from surplus.domain.models import TokenCounts, UsageEvent, UsageSourceKind

SKIP_MODELS = frozenset({"<synthetic>"})


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def parse_record(
    record: dict[str, Any],
    *,
    source: UsageSourceKind = UsageSourceKind.CC_LOG,
    is_agent: bool = False,
    run_id: str | None = None,
    project_id: str | None = None,
) -> UsageEvent | None:
    """Turn one decoded log/stream object into a UsageEvent, or None if it carries no usage."""
    if record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    model = message.get("model")
    if not isinstance(usage, dict) or not isinstance(model, str) or model in SKIP_MODELS:
        return None
    message_id = message.get("id") or record.get("requestId")
    if not isinstance(message_id, str) or not message_id:
        return None
    raw_ts = record.get("timestamp")
    try:
        ts = ensure_utc(datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00")))
    except ValueError:
        return None
    session_id = record.get("sessionId") or record.get("session_id") or ""
    entrypoint = record.get("entrypoint")
    return UsageEvent(
        message_id=message_id,
        ts=ts,
        source=source,
        session_id=str(session_id),
        model=model,
        tokens=TokenCounts(
            input=_int(usage.get("input_tokens")),
            output=_int(usage.get("output_tokens")),
            cache_create=_int(usage.get("cache_creation_input_tokens")),
            cache_read=_int(usage.get("cache_read_input_tokens")),
        ),
        is_agent=is_agent,
        is_sidechain=bool(record.get("isSidechain", False)),
        entrypoint=entrypoint if isinstance(entrypoint, str) else None,
        run_id=run_id,
        project_id=project_id,
    )


@dataclass(frozen=True)
class UsageTag:
    """Attribution for logs written by an agent run (its own CLAUDE_CONFIG_DIR)."""

    run_id: str
    project_id: str
    source: UsageSourceKind = UsageSourceKind.AGENT


def parse_line(line: bytes | str, tag: UsageTag | None = None) -> UsageEvent | None:
    """Parse one JSONL line. Malformed JSON and lines without usage return None."""
    try:
        record = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    if tag is None:
        return parse_record(record)
    return parse_record(
        record, source=tag.source, is_agent=True, run_id=tag.run_id, project_id=tag.project_id
    )


@dataclass(frozen=True)
class FileCursor:
    path: str
    inode: int
    offset: int


@dataclass(frozen=True)
class ReadResult:
    events: list[UsageEvent]
    cursor: FileCursor
    lines_read: int
    bad_lines: int


def read_new_lines(path: Path, cursor: FileCursor | None, tag: UsageTag | None = None) -> ReadResult:
    """Read complete lines appended since `cursor`. Restarts at 0 if the file was replaced or truncated."""
    st = path.stat()
    offset = 0
    if cursor is not None and cursor.inode == st.st_ino and cursor.offset <= st.st_size:
        offset = cursor.offset

    events: list[UsageEvent] = []
    lines = bad = 0
    with path.open("rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # partial last line: leave it for the next pass
            offset += len(raw)
            stripped = raw.strip()
            if not stripped:
                continue
            lines += 1
            # Cheap pre-filter: most lines are not assistant messages with usage.
            if b'"usage"' not in stripped:
                continue
            event = parse_line(stripped, tag)
            if event is None:
                if not _is_json(stripped):
                    bad += 1
                continue
            events.append(event)
    return ReadResult(events, FileCursor(str(path), st.st_ino, offset), lines, bad)


def _is_json(raw: bytes) -> bool:
    try:
        json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True


def iter_log_files(roots: tuple[Path, ...]) -> Iterator[Path]:
    """All session logs under the given roots (recursive, so subagent dirs are included)."""
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                if name.endswith(".jsonl"):
                    yield Path(dirpath) / name
