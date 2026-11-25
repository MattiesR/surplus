"""FakeRuntime: a ContainerRuntime for tests. No Podman, no network, no quota.

`exec` runs the given argv on the host with cwd = the run's workspace (tests control every command), and
`exec_stream` calls a scripted agent that may change the workspace and returns stream-json lines.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from surplus.runner.container import ExecResult, RunContainerSpec

AgentFn = Callable[[Path, list[str], str, dict[str, str]], list[str]]


@dataclass
class Call:
    kind: str
    args: Any


class FakeStream:
    def __init__(self, lines: list[str], rc: int = 0, delay: float = 0.0) -> None:
        self._lines = lines
        self._rc = rc
        self._delay = delay

    async def lines(self) -> AsyncIterator[bytes]:
        for line in self._lines:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield (line + "\n").encode()

    async def wait(self) -> int:
        return self._rc

    def stderr_tail(self) -> str:
        return ""


@dataclass
class FakeRuntime:
    agents: list[AgentFn] = field(default_factory=list)  # one per exec_stream call, in order
    image: bool = True
    line_delay: float = 0.0
    calls: list[Call] = field(default_factory=list)
    specs: dict[str, RunContainerSpec] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)

    async def image_exists(self, image: str) -> bool:
        return self.image

    async def ensure_network(self) -> None:
        self.calls.append(Call("network", None))

    async def ensure_proxy(self, image: str, allow: tuple[str, ...] = ()) -> None:
        self.calls.append(Call("proxy", (image, allow)))

    async def start(self, spec: RunContainerSpec) -> None:
        self.specs[spec.name] = spec
        self.calls.append(Call("start", spec))

    async def exec_stream(
        self, name: str, argv: Sequence[str], stdin: bytes, secrets: Mapping[str, str]
    ) -> FakeStream:
        self.calls.append(Call("exec_stream", {"argv": list(argv), "secrets": dict(secrets)}))
        agent = self.agents.pop(0)
        lines = agent(self.specs[name].workspace, list(argv), stdin.decode(), dict(secrets))
        return FakeStream(lines, delay=self.line_delay)

    async def exec(
        self, name: str, argv: Sequence[str], timeout: float, max_output: int = 200_000
    ) -> ExecResult:
        self.calls.append(Call("exec", list(argv)))
        spec = self.specs[name]
        env = {**os.environ, **spec.env}
        proc = subprocess.run(
            list(argv),
            cwd=spec.workspace,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        return ExecResult(proc.returncode, (proc.stdout + proc.stderr)[-max_output:])

    async def signal_agent(self, name: str) -> None:
        self.signals.append(name)

    async def remove(self, name: str) -> None:
        self.removed.append(name)

    async def exists(self, name: str) -> bool:
        return name in self.specs and name not in self.removed

    async def list_run_containers(self, instance: str) -> dict[str, str]:
        return {}


# ---------------------------------------------------------------- stream-json helpers


def init(session: str = "sess-1", model: str = "claude-sonnet-5-5") -> str:
    return json.dumps({"type": "system", "subtype": "init", "session_id": session, "model": model})


def text(t: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}})


def tool(name: str, **inp: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
    )


def limits(five: float, week: float, resets_5h: int = 1795653000, resets_7d: int = 1796238000) -> str:
    return json.dumps(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "unifiedWindows": {
                    "five_hour": {"utilization": five / 100, "resetsAt": resets_5h, "status": "allowed"},
                    "seven_day": {"utilization": week / 100, "resetsAt": resets_7d, "status": "allowed"},
                },
            },
        }
    )


def result(final: str, subtype: str = "success", turns: int = 3, session: str = "sess-1") -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": subtype,
            "is_error": subtype != "success",
            "num_turns": turns,
            "result": final,
            "session_id": session,
        }
    )


def report_block(**overrides: Any) -> str:
    rep = {
        "summary": "Added hello.txt.",
        "done": ["created hello.txt"],
        "not_done": [],
        "tests": "pass",
        "questions": [
            {"id": "Q1", "text": "Greet in Dutch too?", "options": ["A yes", "B no"], "recommended": "B"}
        ],
        "next_suggestions": [{"title": "Add a goodbye file", "why": "symmetry", "size": "S"}],
    }
    rep.update(overrides)
    return "All done.\n\n```json\n" + json.dumps(rep, indent=2) + "\n```\n"
