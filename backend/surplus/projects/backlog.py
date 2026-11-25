"""backlog.md — strict parser and serialiser with byte-identical round trips (plan §4.1).

Item grammar (one line, canonical spacing, no trailing whitespace):

    - [ ] T-007 (P1, ~S) Add CSV export — acceptance: exports every row
    - [x] T-003 (P2) Set up CI
    - [ ] T-012 (P3, ~M, proposed) Dark mode

    status   "[ ]" open | "[x]" done
    id       T-<digits>, unique in the file
    meta     (P0..P3[, ~S|~M|~L][, flag]*)   flags: proposed, blocked
    title    free text, must not contain " — acceptance: "
    accept.  optional, after " — acceptance: "

Lines indented by two spaces directly under an item are its notes. Every other line (headings, prose, blank
lines) is kept verbatim. A line that starts with "- [" but does not match the grammar is an error with its
line number, so a typo is reported instead of silently dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

ACCEPT_SEP = " — acceptance: "
FLAGS = frozenset({"proposed", "blocked"})

_ITEM_RE = re.compile(
    r"^- \[(?P<done>[ x])\] (?P<id>T-\d{3,}) "
    r"\((?P<prio>P[0-3])(?:, ~(?P<size>[SML]))?(?P<flags>(?:, [a-z]+)*)\) "
    r"(?P<rest>\S.*)$"
)
_ID_RE = re.compile(r"^T-(\d{3,})$")


class Size(StrEnum):
    S = "S"
    M = "M"
    L = "L"


class BacklogError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        self.line = line
        self.message = message
        super().__init__(f"line {line}: {message}")


@dataclass
class BacklogItem:
    id: str
    title: str
    priority: int = 2
    size: Size | None = None
    done: bool = False
    acceptance: str | None = None
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # raw note lines, including their indentation

    @property
    def proposed(self) -> bool:
        return "proposed" in self.flags

    @property
    def number(self) -> int:
        m = _ID_RE.match(self.id)
        return int(m.group(1)) if m else 0

    def validate(self) -> None:
        if not _ID_RE.match(self.id):
            raise ValueError(f"bad task id {self.id!r}")
        if not 0 <= self.priority <= 3:
            raise ValueError("priority must be 0..3")
        for text, what in ((self.title, "title"), (self.acceptance or "", "acceptance")):
            if "\n" in text or "\r" in text:
                raise ValueError(f"{what} must be a single line")
            if text != text.strip():
                raise ValueError(f"{what} has leading or trailing whitespace")
        if not self.title:
            raise ValueError("title is empty")
        if ACCEPT_SEP in self.title or ACCEPT_SEP.strip() in self.title:
            raise ValueError("title must not contain the acceptance separator")
        if self.acceptance is not None and not self.acceptance:
            raise ValueError("acceptance is empty; use None")
        unknown = set(self.flags) - FLAGS
        if unknown:
            raise ValueError(f"unknown flags {sorted(unknown)}")
        for note in self.notes:
            if not note.startswith("  "):
                raise ValueError("notes must be indented by two spaces")

    def render(self) -> list[str]:
        meta = [f"P{self.priority}"]
        if self.size is not None:
            meta.append(f"~{self.size.value}")
        meta.extend(self.flags)
        line = f"- [{'x' if self.done else ' '}] {self.id} ({', '.join(meta)}) {self.title}"
        if self.acceptance is not None:
            line += ACCEPT_SEP + self.acceptance
        return [line, *self.notes]


@dataclass
class Backlog:
    """Lines in file order: plain strings are kept verbatim, items are re-rendered from their fields."""

    blocks: list[str | BacklogItem]
    final_newline: bool = True

    @property
    def items(self) -> list[BacklogItem]:
        return [b for b in self.blocks if isinstance(b, BacklogItem)]

    def get(self, item_id: str) -> BacklogItem:
        for item in self.items:
            if item.id == item_id:
                return item
        raise KeyError(item_id)

    def next_id(self) -> str:
        return f"T-{max((i.number for i in self.items), default=0) + 1:03d}"

    def add(self, item: BacklogItem) -> BacklogItem:
        item.validate()
        if any(i.id == item.id for i in self.items):
            raise ValueError(f"duplicate id {item.id}")
        # Insert after the last item, or at the end of the file when there are none yet.
        last = max((n for n, b in enumerate(self.blocks) if isinstance(b, BacklogItem)), default=None)
        if last is None:
            while self.blocks and self.blocks[-1] == "":
                self.blocks.pop()
            if self.blocks:
                self.blocks.append("")
            self.blocks.append(item)
        else:
            self.blocks.insert(last + 1, item)
        return item

    def remove(self, item_id: str) -> BacklogItem:
        item = self.get(item_id)
        self.blocks.remove(item)
        return item

    def reorder(self, ids: Iterable[str]) -> None:
        """Put the items in the given order. Items keep the slots items had; other lines do not move."""
        order = list(ids)
        current = self.items
        if sorted(order) != sorted(i.id for i in current) or len(set(order)) != len(order):
            raise ValueError("reorder needs exactly the existing ids, each once")
        by_id = {i.id: i for i in current}
        queue = [by_id[i] for i in order]
        self.blocks = [queue.pop(0) if isinstance(b, BacklogItem) else b for b in self.blocks]

    def render(self) -> str:
        lines: list[str] = []
        for b in self.blocks:
            lines.extend(b.render() if isinstance(b, BacklogItem) else [b])
        text = "\n".join(lines)
        return text + "\n" if self.final_newline else text


def parse(text: str) -> Backlog:
    if "\r" in text:
        raise BacklogError(1, "Windows line endings are not supported; save the file with LF line endings")
    final_newline = text.endswith("\n")
    raw_lines = text.split("\n")
    if final_newline:
        raw_lines.pop()
    blocks: list[str | BacklogItem] = []
    seen: set[str] = set()
    current: BacklogItem | None = None
    for lineno, raw in enumerate(raw_lines, start=1):
        if current is not None and raw.startswith("  ") and raw.strip():
            current.notes.append(raw)
            continue
        current = None
        if not raw.startswith("- ["):
            blocks.append(raw)
            continue
        if raw != raw.rstrip():
            raise BacklogError(lineno, "trailing whitespace on a task line")
        m = _ITEM_RE.match(raw)
        if not m:
            raise BacklogError(
                lineno, "task line does not match '- [ ] T-001 (P1, ~S) Title — acceptance: …'"
            )
        flags = [f for f in m.group("flags").split(", ") if f]
        unknown = set(flags) - FLAGS
        if unknown:
            raise BacklogError(lineno, f"unknown flag(s) {', '.join(sorted(unknown))}")
        rest = m.group("rest")
        title, sep, acceptance = rest.partition(ACCEPT_SEP)
        if not sep:
            if ACCEPT_SEP.strip() in title:
                raise BacklogError(lineno, "malformed acceptance text (nothing after 'acceptance:')")
            acceptance_value: str | None = None
        elif not acceptance or acceptance != acceptance.strip() or ACCEPT_SEP in acceptance:
            raise BacklogError(lineno, "malformed acceptance text")
        else:
            acceptance_value = acceptance
        if title != title.strip():
            raise BacklogError(lineno, "title has extra spaces")
        item_id = m.group("id")
        if item_id in seen:
            raise BacklogError(lineno, f"duplicate task id {item_id}")
        seen.add(item_id)
        size = m.group("size")
        current = BacklogItem(
            id=item_id,
            title=title,
            priority=int(m.group("prio")[1]),
            size=Size(size) if size else None,
            done=m.group("done") == "x",
            acceptance=acceptance_value,
            flags=flags,
        )
        blocks.append(current)
    return Backlog(blocks, final_newline)


NEW_BACKLOG = (
    "# Backlog\n\n<!-- Task syntax: - [ ] T-001 (P1, ~S) Title — acceptance: how we know it is done -->\n"
)
