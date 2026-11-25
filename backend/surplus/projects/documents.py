"""goals.md and decisions.md — section-based Markdown documents with lossless round trips.

Both are split on level-2 headings (`## `). Each section keeps its raw text, so `render()` reproduces the file
exactly; structured fields (goal id, acceptance criteria, decision Q/A) are read from the raw text for display
and prompt composition. Edits are whole-file (UI editor) or append-only (decisions).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_GOAL_HEAD_RE = re.compile(r"^## (?:(G\d+)[.:)]\s+)?(.+?)\s*$")
_ACCEPT_HEAD_RE = re.compile(r"^\s*(\*\*)?acceptance( criteria)?:?(\*\*)?:?\s*$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")


@dataclass(frozen=True)
class Section:
    heading: str  # the "## …" line without the newline
    raw: str  # full text of the section, heading line included


def split_sections(text: str) -> tuple[str, list[Section]]:
    """(preamble, sections). Lines starting with '## ' open a new section (fenced code is respected)."""
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if stripped.lstrip().startswith("```"):
            in_fence = not in_fence
        if not in_fence and stripped.startswith("## "):
            sections.append((stripped, [line]))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return "".join(preamble), [Section(h, "".join(lines)) for h, lines in sections]


# ---------------------------------------------------------------- goals.md


@dataclass(frozen=True)
class Goal:
    id: str  # "G1", or a positional id when the heading has none
    title: str
    acceptance: list[str]
    body: str  # section text without the heading line


@dataclass(frozen=True)
class GoalsDoc:
    preamble: str
    sections: list[Section]

    @property
    def goals(self) -> list[Goal]:
        out: list[Goal] = []
        for n, sec in enumerate(self.sections, start=1):
            m = _GOAL_HEAD_RE.match(sec.heading)
            gid = (m.group(1) if m and m.group(1) else f"G{n}") if m else f"G{n}"
            title = m.group(2) if m else sec.heading[3:]
            body = sec.raw.split("\n", 1)[1] if "\n" in sec.raw else ""
            out.append(Goal(gid, title, _acceptance(body), body))
        return out

    def render(self) -> str:
        return self.preamble + "".join(s.raw for s in self.sections)


def _acceptance(body: str) -> list[str]:
    criteria: list[str] = []
    collecting = False
    for line in body.splitlines():
        if _ACCEPT_HEAD_RE.match(line):
            collecting = True
            continue
        if collecting:
            m = _BULLET_RE.match(line)
            if m:
                criteria.append(m.group(1))
            elif line.strip():
                collecting = False
    return criteria


def parse_goals(text: str) -> GoalsDoc:
    preamble, sections = split_sections(text)
    return GoalsDoc(preamble, sections)


def render_new_goals(goals: list[tuple[str, list[str]]]) -> str:
    """goals.md for the new-project wizard: ordered goals, each with acceptance criteria."""
    parts = ["# Goals\n", "\n", "<!-- Ordered by importance. Each goal lists how we know it is done. -->\n"]
    for n, (title, criteria) in enumerate(goals, start=1):
        parts.append(f"\n## G{n}. {_one_line(title)}\n")
        if criteria:
            parts.append("\nAcceptance:\n")
            parts.extend(f"- {_one_line(c)}\n" for c in criteria)
    return "".join(parts)


# ---------------------------------------------------------------- decisions.md


@dataclass(frozen=True)
class Decision:
    heading: str
    question: str | None
    answer: str | None
    body: str


def parse_decisions(text: str) -> list[Decision]:
    _, sections = split_sections(text)
    out = []
    for sec in sections:
        body = sec.raw.split("\n", 1)[1] if "\n" in sec.raw else ""
        out.append(
            Decision(sec.heading[3:].strip(), _field(body, "Question"), _field(body, "Answer"), body.strip())
        )
    return out


def _field(body: str, name: str) -> str | None:
    m = re.search(rf"^\*\*{name}:\*\*\s*(.+)$", body, re.MULTILINE)
    return m.group(1).strip() if m else None


def append_decision(text: str, when: datetime, question: str, answer: str, ref: str | None = None) -> str:
    """Append-only: returns `text` with one new entry at the end (existing bytes untouched)."""
    stamp = when.strftime("%Y-%m-%d %H:%M UTC")
    heading = f"## {stamp}" + (f" · {_one_line(ref)}" if ref else "")
    entry = f"{heading}\n\n**Question:** {_one_line(question)}\n**Answer:** {_one_line(answer)}\n"
    if text and not text.endswith("\n"):
        text += "\n"
    return text + ("\n" if text else "") + entry


def _one_line(text: str) -> str:
    return " ".join(text.split())


NEW_DECISIONS = (
    "# Decisions\n\n<!-- Answers to agent questions. Append-only: Surplus adds entries at the end. -->\n"
)
