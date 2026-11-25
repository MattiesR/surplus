from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from surplus.projects import frontmatter as fm

PLAN_EXAMPLE = """---
id: my-cool-idea
name: My Cool Idea
status: active          # active | paused | archived
priority: 2             # 1 = highest
model: sonnet           # default model for runs; opus allowed only if a budget flag permits
max_share_of_surplus: 0.5   # this project may use at most 50% of surplus capacity
test_command: "uv run pytest -q"
---

# My Cool Idea

Body text.
"""


def test_plan_example_parses() -> None:
    doc = fm.parse(PLAN_EXAMPLE)
    assert doc.values == {
        "id": "my-cool-idea",
        "name": "My Cool Idea",
        "status": "active",
        "priority": 2,
        "model": "sonnet",
        "max_share_of_surplus": 0.5,
        "test_command": "uv run pytest -q",
    }
    assert doc.body.startswith("\n# My Cool Idea")


def test_round_trip_is_byte_identical() -> None:
    assert fm.parse(PLAN_EXAMPLE).render() == PLAN_EXAMPLE


def test_set_preserves_comments_and_alignment() -> None:
    doc = fm.parse(PLAN_EXAMPLE)
    doc.set("status", "paused")
    doc.set("priority", 1)
    out = doc.render()
    assert "status: paused          # active | paused | archived\n" in out
    assert "priority: 1             # 1 = highest\n" in out
    restored = out.replace("status: paused ", "status: active ").replace("priority: 1 ", "priority: 2 ")
    assert restored == PLAN_EXAMPLE


def test_set_new_key_and_quoting() -> None:
    doc = fm.parse("---\nid: x\n---\n")
    doc.set("test_command", "make test # all")
    doc.set("name", "true")
    doc.set("flag", True)
    out = doc.render()
    assert 'test_command: "make test # all"' in out
    assert 'name: "true"' in out
    assert "flag: true" in out
    assert fm.parse(out).values == {
        "id": "x",
        "test_command": "make test # all",
        "name": "true",
        "flag": True,
    }


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("no front matter\n", 1),
        ("---\nid: x\n", 3),
        ("---\nid: [1, 2]\n---\n", 2),
        ("---\njust text\n---\n", 2),
        ("---\nid: x\nid: y\n---\n", 3),
        ('---\nname: "unterminated\n---\n', 2),
        ('---\nname: "a" trailing\n---\n', 2),
    ],
)
def test_strict_errors_have_line_numbers(text: str, line: int) -> None:
    with pytest.raises(fm.FrontMatterError) as exc:
        fm.parse(text)
    assert exc.value.line == line


def test_closing_at_eof_and_crlf_round_trip() -> None:
    for text in ("---\nid: x\n---", "---\r\nid: x\r\n---\r\nbody\r\n"):
        assert fm.parse(text).render() == text


scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\n\r"), max_size=40),
)


@given(st.dictionaries(st.from_regex(r"[a-z_][a-z0-9_]{0,10}", fullmatch=True), scalars, max_size=6))
def test_set_then_parse_preserves_values(values: dict[str, fm.Scalar]) -> None:
    doc = fm.parse("---\n# header comment\n---\nbody\n")
    for k, v in values.items():
        doc.set(k, v)
    text = doc.render()
    again = fm.parse(text)
    assert again.values == values
    assert again.render() == text
