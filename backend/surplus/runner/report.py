"""Run report: schema, extraction from the agent's final message, fallback, Markdown rendering — pure."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MAX_TEXT = 2000


def _one_line(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


class Question(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(pattern=r"^Q\d{1,3}$")
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    options: list[str] = Field(default_factory=list, max_length=6)
    recommended: str | None = None


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = Field(min_length=1, max_length=200)
    why: str = Field(default="", max_length=MAX_TEXT)
    size: Literal["S", "M", "L"] = "M"

    @field_validator("title")
    @classmethod
    def _clean_title(cls, v: str) -> str:
        # Backlog lines are single-line and use " — acceptance: " as a separator.
        v = _one_line(v, 200).replace(" — acceptance:", " - acceptance:")
        if not v:
            raise ValueError("empty title")
        return v


class RunReport(BaseModel):
    """The JSON block every run must end with (plan §8.5). `tests` is the agent's own claim."""

    model_config = ConfigDict(extra="ignore")
    summary: str = Field(min_length=1, max_length=4000)
    done: list[str] = Field(default_factory=list, max_length=50)
    not_done: list[str] = Field(default_factory=list, max_length=50)
    tests: Literal["pass", "fail", "none"] = "none"
    questions: list[Question] = Field(default_factory=list, max_length=10)
    next_suggestions: list[Suggestion] = Field(default_factory=list, max_length=10)


def report_json_schema() -> dict[str, object]:
    return RunReport.model_json_schema()


_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _candidates(text: str) -> list[str]:
    """JSON-looking chunks, last first: fenced blocks, then balanced {...} objects containing "summary"."""
    found = [m.group(1) for m in _FENCE.finditer(text)]
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"' and depth > 0:
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and '"summary"' in text[start : i + 1]:
                found.append(text[start : i + 1])
    return list(reversed(found))


@dataclass(frozen=True)
class Extraction:
    report: RunReport | None
    error: str | None


def extract_report(text: str) -> Extraction:
    """Find the last valid report JSON in the agent's final message."""
    if not text.strip():
        return Extraction(None, "the final message is empty")
    last_error = 'no JSON block with a "summary" field found'
    for chunk in _candidates(text):
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError as exc:
            last_error = f"invalid JSON: {exc.msg} (line {exc.lineno})"
            continue
        try:
            return Extraction(RunReport.model_validate(data), None)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(p) for p in first["loc"])
            last_error = f"report does not match the schema: {loc}: {first['msg']}"
    return Extraction(None, last_error)


def repair_prompt(error: str) -> str:
    return (
        "Your final message did not contain a valid report: "
        f"{error}. Reply with ONLY the report as one ```json fenced block that matches the schema "
        "from the run rules (summary, done, not_done, tests, questions, next_suggestions). "
        "Do not run any tools."
    )


def fallback_report(commits: list[str], files_changed: list[str], verified_tests: str) -> RunReport:
    """Used when the agent's report is missing and the repair turn failed too."""
    n = len(commits)
    files = ", ".join(files_changed[:10]) + (" …" if len(files_changed) > 10 else "")
    summary = "The agent did not return a valid report; this one was generated from the branch. " + (
        f"{n} commit(s); files changed: {files or 'none'}." if n else "No commits were made."
    )
    tests: Literal["pass", "fail", "none"] = verified_tests if verified_tests in ("pass", "fail") else "none"  # type: ignore[assignment]
    return RunReport(summary=summary, done=[_one_line(c, 200) for c in commits[:20]], tests=tests)


ReportSource = Literal["agent", "repaired", "fallback"]


@dataclass(frozen=True)
class RunFacts:
    """What Surplus itself observed about a run (as opposed to what the agent claims)."""

    run_id: str
    project_id: str
    task_id: str | None
    task_title: str | None
    model: str
    branch: str | None
    started_at: datetime | None
    ended_at: datetime | None
    state: str
    exit_reason: str | None
    verified_tests: str  # pass | fail | none | error
    spent_pct_est: float | None
    commits: int
    report_source: ReportSource


def _list(items: list[str]) -> str:
    return "\n".join(f"- {_one_line(i, 500)}" for i in items) if items else "_none_"


def render_run_md(facts: RunFacts, report: RunReport, test_output: str = "") -> str:
    """Human-readable `.surplus/runs/<stamp>-<id>.md`."""

    def fmt(dt: datetime | None) -> str:
        return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"

    lines = [
        f"# Run {facts.run_id}"
        + (f" · {facts.task_id} {_one_line(facts.task_title or '', 120)}" if facts.task_id else ""),
        "",
        f"- Project: `{facts.project_id}` · model: {facts.model} · branch: `{facts.branch or '—'}`",
        f"- Started: {fmt(facts.started_at)} · ended: {fmt(facts.ended_at)}",
        f"- Outcome: {facts.state}" + (f" ({facts.exit_reason})" if facts.exit_reason else ""),
        f"- Tests (run by Surplus): **{facts.verified_tests}** · agent says: {report.tests}",
        f"- Commits: {facts.commits}"
        + (
            f" · est. spend: {facts.spent_pct_est:.1f} pp of the 5-hour window"
            if facts.spent_pct_est is not None
            else ""
        ),
        f"- Report source: {facts.report_source}",
        "",
        "## Summary",
        "",
        report.summary.strip(),
        "",
        "## Done",
        "",
        _list(report.done),
        "",
        "## Not done",
        "",
        _list(report.not_done),
        "",
        "## Questions",
        "",
    ]
    if report.questions:
        for q in report.questions:
            lines.append(f"- **{q.id}** {_one_line(q.text, 1000)}")
            lines.extend(f"  - {_one_line(o, 300)}" for o in q.options)
            if q.recommended:
                lines.append(f"  - recommended: {_one_line(q.recommended, 300)}")
    else:
        lines.append("_none_")
    lines += ["", "## Suggestions", ""]
    if report.next_suggestions:
        lines.extend(
            f"- (~{s.size}) {s.title}" + (f" — {_one_line(s.why, 500)}" if s.why else "")
            for s in report.next_suggestions
        )
    else:
        lines.append("_none_")
    if test_output.strip():
        tail = test_output.strip()[-4000:]
        fence = "````" if "```" in tail else "```"
        lines += ["", "## Test output (tail)", "", fence, tail, fence]
    return "\n".join(lines) + "\n"
