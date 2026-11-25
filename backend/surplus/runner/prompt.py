"""Prompt composition for one run (plan §8.2) — pure: snapshot + task in, two strings out."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from surplus.projects.backlog import BacklogItem
from surplus.projects.documents import Decision, Goal

PROMPTS_DIR = Path(__file__).parent / "prompts"
MAX_DECISIONS = 15
MAX_RECENT_RUNS = 3

_env = Environment(
    loader=FileSystemLoader(PROMPTS_DIR),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,  # noqa: S701 - renders Markdown for the CLI, never HTML
)

REPORT_EXAMPLE = {
    "summary": "One paragraph: what changed and why.",
    "done": ["Implemented …", "Added tests for …"],
    "not_done": ["What is still missing"],
    "tests": "pass",
    "questions": [
        {"id": "Q1", "text": "Should …?", "options": ["A …", "B …"], "recommended": "A"},
    ],
    "next_suggestions": [{"title": "Follow-up task", "why": "Because …", "size": "S"}],
}


@dataclass(frozen=True)
class RunPrompt:
    prompt: str
    system_append: str


def decision_lines(decisions: list[Decision], task_id: str) -> list[str]:
    """Decisions that mention the task first, then the most recent ones, as single lines."""

    def line(d: Decision) -> str:
        if d.question and d.answer:
            return f"{d.heading}: Q: {d.question} → A: {d.answer}"
        return f"{d.heading}: {' '.join(d.body.split())[:400]}"

    related = [d for d in decisions if task_id in d.heading or task_id in d.body]
    rest = [d for d in reversed(decisions) if d not in related]
    return [line(d) for d in [*related, *rest][:MAX_DECISIONS]]


_SUMMARY_RE = re.compile(r"^## Summary\s*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def run_summary(name: str, text: str) -> str:
    m = _SUMMARY_RE.search(text)
    body = " ".join((m.group(1) if m else "").split())[:500]
    return f"{name.removesuffix('.md')}: {body or '(no summary)'}"


def compose(
    *,
    name: str,
    description: str,
    goals: list[Goal],
    task: BacklogItem,
    decisions: list[Decision],
    recent_runs: list[tuple[str, str]],
    branch: str,
    test_command: str | None,
    max_turns: int,
) -> RunPrompt:
    """`recent_runs` is [(filename, markdown)] newest first."""
    prompt = _env.get_template("run.md.j2").render(
        name=name,
        description=description.strip() or "(no description)",
        goals=goals,
        task=task,
        notes="\n".join(task.notes),
        decisions=decision_lines(decisions, task.id),
        recent_runs=[run_summary(n, t) for n, t in recent_runs[:MAX_RECENT_RUNS]],
    )
    rules = _env.get_template("rules.md.j2").render(
        branch=branch,
        task_id=task.id,
        test_command=test_command,
        max_turns=max_turns,
        report_example=json.dumps(REPORT_EXAMPLE, indent=2, ensure_ascii=False),
    )
    return RunPrompt(prompt=prompt, system_append=rules)


def branch_name(task: BacklogItem) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.title.lower()).strip("-")[:40].rstrip("-")
    return f"surplus/{task.id}-{slug}" if slug else f"surplus/{task.id}"


STOP_EXPLANATIONS = {
    "budget_exhausted": "it reached the 5-hour hard stop",
    "max_turns": "it used all its turns",
    "timeout": "it reached the wall-clock limit",
}


def continuation_prompt(task: BacklogItem, exit_reason: str, cap_five_hour_pct: float, max_turns: int) -> str:
    """The user message that resumes a stopped run's session after the user allowed it to continue."""
    why = STOP_EXPLANATIONS.get(exit_reason, "it was stopped")
    return (
        f"Your previous run on {task.id} was stopped because {why}. Surplus committed and tested what you "
        "had (any uncommitted files were committed for you), and the user has now allowed you to continue: "
        f"the 5-hour hard stop for this continuation is {cap_five_hour_pct:.0f}%, and you have {max_turns} "
        "more turns. Check `git log` and `git status`, continue the task from where you stopped, commit your "
        "work, and end with the report JSON block exactly as the rules describe. The report covers the whole "
        "task, including what the previous run did."
    )
