"""Wizard helper: "help me write acceptance criteria" — one small, user-triggered Claude call (plan §11).

It runs the official CLI with Haiku, one turn, no tools, from a fixed folder. The session is persisted on
purpose: the collector then ingests its usage like any other manual Claude Code usage, so the spend is
accounted for.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

MAX_INPUT_CHARS = 6000


class GoalDraft(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    acceptance: list[str] = Field(default_factory=list, max_length=8)


class AssistError(RuntimeError):
    pass


def build_prompt(name: str, description: str, goals: list[str]) -> str:
    listed = "\n".join(f"{n}. {g}" for n, g in enumerate(goals, start=1)) or "(none yet: propose 3)"
    return (
        "You help define a small software side project. For each goal, write 2-4 short, testable acceptance "
        "criteria (one line each, observable behaviour, no implementation details). "
        "Keep the goals' order and "
        "wording; if no goals are given, propose 3 goals.\n"
        'Reply with only JSON: {"goals": [{"title": "...", "acceptance": ["...", "..."]}]}\n\n'
        f"Project: {name}\nDescription: {description}\nGoals:\n{listed}"
    )[:MAX_INPUT_CHARS]


def parse_reply(result_text: str) -> list[GoalDraft]:
    match = re.search(r"\{.*\}", result_text, re.DOTALL)
    if not match:
        raise AssistError("Claude did not return JSON")
    try:
        data = json.loads(match.group(0))
        return [GoalDraft.model_validate(g) for g in data["goals"]][:10]
    except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
        raise AssistError(f"could not read Claude's reply: {exc}") from exc


async def suggest_acceptance(
    claude_bin: str, workdir: Path, name: str, description: str, goals: list[str], timeout: float = 90
) -> list[GoalDraft]:
    workdir.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        claude_bin,
        "-p",
        build_prompt(name, description, goals),
        "--model",
        "haiku",
        "--max-turns",
        "1",
        "--tools",
        "",
        "--strict-mcp-config",
        "--output-format",
        "json",
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise AssistError("Claude did not answer in time") from exc
    if proc.returncode != 0:
        raise AssistError(f"claude exited with {proc.returncode}: {err.decode(errors='replace')[-300:]}")
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError as exc:
        raise AssistError("unexpected CLI output") from exc
    if envelope.get("is_error"):
        raise AssistError(str(envelope.get("result") or "Claude returned an error"))
    return parse_reply(str(envelope.get("result", "")))
