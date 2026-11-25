"""Parse the CLI's `--output-format stream-json` lines during a run — pure.

Each line becomes zero or more `LogEntry`s for the live log, plus limit readings from `rate_limit_event`s,
and the final `result` is captured. Usage tokens are not taken from the stream (its records carry no
timestamp); they are ingested from the run's own session JSONL, which has the same `message.id`s.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from surplus.collector.limits import LimitFormatError, parse_rate_limit_event
from surplus.domain.models import LimitReading

LogKind = Literal["system", "text", "tool", "tool_result", "limit", "result", "error", "surplus"]
MAX_ENTRY = 2000


@dataclass(frozen=True)
class LogEntry:
    ts: datetime
    kind: LogKind
    text: str
    is_error: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"ts": self.ts.isoformat(), "kind": self.kind, "text": self.text, "is_error": self.is_error}


@dataclass
class StreamState:
    session_id: str | None = None
    model: str | None = None
    final_text: str = ""
    result_subtype: str | None = None  # success | error_max_turns | error_during_execution …
    is_error: bool = False
    num_turns: int = 0
    readings: dict[str, LimitReading] = field(default_factory=dict)  # latest per bucket
    first_readings: dict[str, LimitReading] = field(default_factory=dict)
    bad_lines: int = 0

    @property
    def finished(self) -> bool:
        return self.result_subtype is not None


@dataclass(frozen=True)
class LineResult:
    entries: list[LogEntry]
    readings: list[LimitReading]


def _clip(text: str, limit: int = MAX_ENTRY) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_summary(name: str, inp: Any) -> str:
    if not isinstance(inp, dict):
        return name
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description"):
        val = inp.get(key)
        if isinstance(val, str) and val:
            return f"{name}: {_clip(val, 400)}"
    return name


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text"
        )
    return ""


def handle_line(state: StreamState, line: str | bytes, ts: datetime) -> LineResult:
    """Update `state` with one stream line and return what to show and store."""
    try:
        rec = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
        if not text.strip():
            return LineResult([], [])
        state.bad_lines += 1
        return LineResult([LogEntry(ts, "error", _clip(text, 500), is_error=True)], [])
    if not isinstance(rec, dict):
        return LineResult([], [])
    kind = rec.get("type")
    entries: list[LogEntry] = []
    readings: list[LimitReading] = []

    if kind == "system" and rec.get("subtype") == "init":
        state.session_id = str(rec.get("session_id") or "") or state.session_id
        state.model = rec.get("model") if isinstance(rec.get("model"), str) else state.model
        entries.append(LogEntry(ts, "system", f"session started · model {state.model or '?'}"))
    elif kind == "assistant":
        msg = rec.get("message") or {}
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text", "")).strip():
                entries.append(LogEntry(ts, "text", _clip(str(block["text"]))))
            elif block.get("type") == "tool_use":
                entries.append(
                    LogEntry(ts, "tool", _tool_summary(str(block.get("name")), block.get("input")))
                )
    elif kind == "user":
        msg = rec.get("message") or {}
        content = msg.get("content")
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = _tool_result_text(block.get("content"))
                err = bool(block.get("is_error"))
                if text.strip() or err:
                    entries.append(LogEntry(ts, "tool_result", _clip(text, 800), is_error=err))
    elif kind == "rate_limit_event":
        try:
            readings = parse_rate_limit_event(rec, ts)
        except LimitFormatError as exc:
            entries.append(LogEntry(ts, "error", f"unreadable rate_limit_event: {exc}", is_error=True))
        for r in readings:
            state.first_readings.setdefault(r.bucket, r)
            state.readings[r.bucket] = r
        if readings:
            entries.append(
                LogEntry(ts, "limit", " · ".join(f"{r.bucket} {r.used_pct:.0f}%" for r in readings))
            )
    elif kind == "result":
        state.result_subtype = str(rec.get("subtype") or "unknown")
        state.is_error = bool(rec.get("is_error"))
        state.num_turns = int(rec.get("num_turns") or 0)
        state.final_text = str(rec.get("result") or "")
        state.session_id = str(rec.get("session_id") or "") or state.session_id
        entries.append(
            LogEntry(
                ts,
                "result",
                f"agent finished: {state.result_subtype} after {state.num_turns} turn(s)",
                is_error=state.is_error,
            )
        )
    return LineResult(entries, readings)


def spent_pct(state: StreamState, bucket: str, before: float | None) -> float | None:
    """Percentage points of `bucket` used since the run started (includes any concurrent manual use)."""
    last = state.readings.get(bucket)
    if last is None:
        return None
    start = before if before is not None else state.first_readings[bucket].used_pct
    return max(0.0, last.used_pct - start)
