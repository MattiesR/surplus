"""Strict, comment-preserving front matter for project.md (plan §4.1).

Supported: a block delimited by `---` lines holding flat `key: scalar  # comment` lines, blank lines and `#`
comment lines. Scalars: bare words/numbers/booleans/null, "double-quoted" (JSON escapes) or 'single-quoted'.
Anything else (nesting, lists, multi-line values) is a `FrontMatterError` with a line number.

Edits replace only the value's characters, so comments, alignment and key order survive byte for byte, and
`parse(text).render() == text` for every file that parses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

Scalar = str | int | float | bool | None

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(\s*)(.*)$")
_INT_RE = re.compile(r"^[-+]?\d+$")
_FLOAT_RE = re.compile(r"^[-+]?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][-+]?\d+)$")
_SAFE_BARE_RE = re.compile(r"^[A-Za-z0-9_./~@+-][A-Za-z0-9_./~@+ -]*$")


class FrontMatterError(ValueError):
    def __init__(self, message: str, line: int | None = None) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}" if line is not None else message)


@dataclass
class _Line:
    raw: str  # without the trailing newline
    key: str | None = None
    value_start: int = 0
    value_end: int = 0


@dataclass
class FrontMatter:
    lines: list[_Line]
    body: str
    newline: str = "\n"
    values: dict[str, Scalar] = field(default_factory=dict)
    closing_newline: bool = True  # False when the file ends right after the closing '---'

    def get(self, key: str, default: Scalar = None) -> Scalar:
        return self.values.get(key, default)

    def set(self, key: str, value: Scalar) -> None:
        rendered = format_scalar(value)
        for line in self.lines:
            if line.key == key:
                line.raw = line.raw[: line.value_start] + rendered + line.raw[line.value_end :]
                line.value_end = line.value_start + len(rendered)
                self.values[key] = value
                return
        raw = f"{key}: {rendered}"
        self.lines.append(_Line(raw, key, len(key) + 2, len(raw)))
        self.values[key] = value

    def render(self) -> str:
        nl = self.newline
        head = nl.join(["---", *(line.raw for line in self.lines), "---"])
        return head + (nl if self.closing_newline else "") + self.body


def parse_scalar(text: str, lineno: int | None = None) -> tuple[Scalar, int]:
    """Parse a scalar at the start of `text`; return (value, length of the value's characters)."""
    if text.startswith('"'):
        i, escaped = 1, False
        while i < len(text):
            ch = text[i]
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                try:
                    return json.loads(text[: i + 1]), i + 1
                except json.JSONDecodeError as exc:
                    raise FrontMatterError(f"bad string escape: {exc.msg}", lineno) from exc
            i += 1
        raise FrontMatterError("unterminated double-quoted string", lineno)
    if text.startswith("'"):
        i = 1
        while i < len(text):
            if text[i] == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                return text[1:i].replace("''", "'"), i + 1
            i += 1
        raise FrontMatterError("unterminated single-quoted string", lineno)
    # Bare value: up to a " #" comment, trailing spaces excluded.
    cut = text.find(" #")
    raw = (text if cut < 0 else text[:cut]).rstrip()
    if raw.startswith(("[", "{", "|", ">", "&", "*", "!")):
        raise FrontMatterError("only flat scalar values are supported", lineno)
    return _bare(raw), len(raw)


def _bare(raw: str) -> Scalar:
    low = raw.lower()
    if low in ("", "null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT_RE.match(raw):
        return int(raw)
    if _FLOAT_RE.match(raw):
        return float(raw)
    return raw


def format_scalar(value: Scalar) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if _SAFE_BARE_RE.match(value) and value == value.strip() and isinstance(_bare(value), str):
        return value
    return json.dumps(value, ensure_ascii=False)


def parse(text: str) -> FrontMatter:
    newline = "\r\n" if text.startswith("---\r\n") else "\n"
    if not text.startswith("---" + newline):
        raise FrontMatterError("file must start with a '---' front-matter line", 1)
    rest = text[len("---" + newline) :]
    lines: list[_Line] = []
    values: dict[str, Scalar] = {}
    pos = 0
    lineno = 1
    while True:
        lineno += 1
        end = rest.find(newline, pos)
        if end < 0:
            if rest[pos:] == "---":  # closing delimiter at EOF without a newline
                return FrontMatter(lines, "", newline, values, closing_newline=False)
            raise FrontMatterError("front matter is not closed with '---'", lineno)
        raw = rest[pos:end]
        pos = end + len(newline)
        if raw == "---":
            return FrontMatter(lines, rest[pos:], newline, values)
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(_Line(raw))
            continue
        m = _KEY_RE.match(raw)
        if not m:
            raise FrontMatterError(f"expected 'key: value', got {raw!r}", lineno)
        key = m.group(1)
        if key in values:
            raise FrontMatterError(f"duplicate key {key!r}", lineno)
        start = len(key) + 1 + len(m.group(2))
        value, length = parse_scalar(m.group(3), lineno)
        trailing = m.group(3)[length:]
        if trailing.strip() and not trailing.lstrip().startswith("#"):
            raise FrontMatterError(f"unexpected text after value: {trailing.strip()!r}", lineno)
        values[key] = value
        lines.append(_Line(raw, key, start, start + length))
