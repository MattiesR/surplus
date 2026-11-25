"""The `get_usage` probe (ADR 0002): reads every bucket's percent and reset time without any inference.

It starts the official CLI in stream-json input mode, sends `initialize` and `get_usage` control requests,
and closes stdin **without ever sending a user message**, so no model call is made and no quota is spent.
Surplus never touches the CLI's credentials; the CLI reads its own login as usual (plan §9.4).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping
from typing import Any

from surplus.clock import Clock
from surplus.collector.limits import LimitFormatError, parse_get_usage
from surplus.domain.models import UsageSnapshot

PROBE_ARGS = (
    "-p",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
    "--no-session-persistence",
    "--strict-mcp-config",
)


class ProbeError(RuntimeError):
    pass


def control_requests() -> bytes:
    reqs = [
        {"type": "control_request", "request_id": "surplus-init", "request": {"subtype": "initialize"}},
        {
            "type": "control_request",
            "request_id": "surplus-usage",
            "request": {"subtype": "get_usage", "skip_behaviors": True},
        },
    ]
    return b"".join(json.dumps(r).encode() + b"\n" for r in reqs)


def extract_usage_response(line: bytes) -> Mapping[str, Any] | None:
    """Return the inner usage payload if `line` is the control_response to our get_usage request."""
    try:
        msg = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(msg, dict) or msg.get("type") != "control_response":
        return None
    resp = msg.get("response")
    if not isinstance(resp, dict) or resp.get("request_id") != "surplus-usage":
        return None
    if resp.get("subtype") != "success":
        raise ProbeError(f"get_usage failed: {resp.get('error') or resp}")
    payload = resp.get("response")
    if not isinstance(payload, dict):
        raise ProbeError("get_usage returned no payload")
    return payload


async def run_probe(claude_bin: str, clock: Clock, timeout: float = 45.0) -> UsageSnapshot:
    proc = await asyncio.create_subprocess_exec(
        claude_bin,
        *PROBE_ARGS,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    if stdin is None or stdout is None or stderr is None:
        raise ProbeError("could not open pipes to the CLI")
    # Drain stderr concurrently so a chatty CLI can never block on a full pipe.
    err_task = asyncio.create_task(stderr.read(64 * 1024))

    async def _read() -> Mapping[str, Any]:
        while True:
            line = await stdout.readline()
            if not line:
                err = (await err_task).decode(errors="replace").strip()
                raise ProbeError(f"CLI exited before answering get_usage: {err[-500:] or 'no output'}")
            payload = extract_usage_response(line)
            if payload is not None:
                return payload

    try:
        stdin.write(control_requests())
        await stdin.drain()
        try:
            payload = await asyncio.wait_for(_read(), timeout)
        except TimeoutError as exc:
            raise ProbeError(f"probe timed out after {timeout}s") from exc
    finally:
        # Closing stdin ends the session; no user message was ever sent.
        with contextlib.suppress(Exception):
            stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        err_task.cancel()
    try:
        return parse_get_usage(payload, clock.now())
    except LimitFormatError as exc:
        raise ProbeError(f"usage format changed: {exc}") from exc
