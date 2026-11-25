from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from surplus.projects import backlog as bl

SAMPLE = """# Backlog

Some prose the user wrote.

- [ ] T-007 (P1, ~S) Add CSV export — acceptance: exports every row
  - note: use the stdlib csv module
- [x] T-003 (P2) Set up CI
- [ ] T-012 (P3, ~M, proposed) Dark mode

## Later
- [ ] T-020 (P0, ~L, blocked) Rewrite the parser — acceptance: all fixtures pass
"""


def test_parse_fields() -> None:
    doc = bl.parse(SAMPLE)
    items = {i.id: i for i in doc.items}
    t7 = items["T-007"]
    assert (t7.priority, t7.size, t7.done, t7.acceptance) == (1, bl.Size.S, False, "exports every row")
    assert t7.notes == ["  - note: use the stdlib csv module"]
    assert items["T-003"].done and items["T-003"].size is None and items["T-003"].acceptance is None
    assert items["T-012"].proposed
    assert items["T-020"].flags == ["blocked"]


def test_round_trip_is_byte_identical() -> None:
    assert bl.parse(SAMPLE).render() == SAMPLE
    for text in ("", "\n", "# Backlog", bl.NEW_BACKLOG, "- [ ] T-001 (P1) x"):
        assert bl.parse(text).render() == text


@pytest.mark.parametrize(
    ("line", "msg"),
    [
        ("- [ ] T-1 (P1) too short id", "does not match"),
        ("- [X] T-001 (P1) capital X", "does not match"),
        ("- [ ]  T-001 (P1) double space", "does not match"),
        ("- [ ] T-001 (P9) bad priority", "does not match"),
        ("- [ ] T-001 (P1, ~XL) bad size", "does not match"),
        ("- [ ] T-001 (P1, urgent) unknown flag", "unknown flag"),
        ("- [ ] T-001 (P1) trailing space ", "trailing whitespace"),
        ("- [ ] T-001 (P1) x — acceptance: ", "trailing whitespace"),
        ("- [ ] T-001 (P1) x — acceptance:", "malformed acceptance"),
    ],
)
def test_strict_errors(line: str, msg: str) -> None:
    with pytest.raises(bl.BacklogError) as exc:
        bl.parse(f"# Backlog\n\n{line}\n")
    assert exc.value.line == 3
    assert msg in exc.value.message


def test_duplicate_ids_rejected() -> None:
    with pytest.raises(bl.BacklogError, match="duplicate"):
        bl.parse("- [ ] T-001 (P1) a\n- [ ] T-001 (P1) b\n")


def test_add_assigns_next_id_after_last_item() -> None:
    doc = bl.parse(SAMPLE)
    item = doc.add(bl.BacklogItem(id=doc.next_id(), title="New thing", priority=2, size=bl.Size.M))
    assert item.id == "T-021"
    out = doc.render()
    assert out.endswith(
        "- [ ] T-020 (P0, ~L, blocked) Rewrite the parser — acceptance: all fixtures pass\n"
        "- [ ] T-021 (P2, ~M) New thing\n"
    )
    assert bl.parse(out).render() == out


def test_add_to_empty_backlog() -> None:
    doc = bl.parse(bl.NEW_BACKLOG)
    doc.add(bl.BacklogItem(id=doc.next_id(), title="First", priority=1))
    assert doc.render() == bl.NEW_BACKLOG + "\n- [ ] T-001 (P1) First\n"


def test_reorder_moves_items_with_notes_and_keeps_other_lines() -> None:
    doc = bl.parse(SAMPLE)
    doc.reorder(["T-020", "T-003", "T-012", "T-007"])
    out = doc.render()
    assert [i.id for i in bl.parse(out).items] == ["T-020", "T-003", "T-012", "T-007"]
    assert "## Later\n- [ ] T-007 (P1, ~S) Add CSV export — acceptance: exports every row\n  - note:" in out
    assert out.startswith("# Backlog\n\nSome prose the user wrote.\n\n- [ ] T-020")
    with pytest.raises(ValueError, match="exactly"):
        doc.reorder(["T-020"])


def test_item_validation() -> None:
    with pytest.raises(ValueError, match="single line"):
        bl.BacklogItem(id="T-001", title="a\nb").validate()
    with pytest.raises(ValueError, match="separator"):
        bl.BacklogItem(id="T-001", title="x — acceptance: y").validate()


titles = (
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zl", "Zp"), blacklist_characters="\n\r  "),
        min_size=1,
        max_size=30,
    )
    .map(lambda s: " ".join(s.split()))
    .filter(lambda s: s and "— acceptance:" not in s)
)

items = st.builds(
    bl.BacklogItem,
    id=st.just("T-000"),
    title=titles,
    priority=st.integers(0, 3),
    size=st.one_of(st.none(), st.sampled_from(list(bl.Size))),
    done=st.booleans(),
    acceptance=st.one_of(st.none(), titles),
    flags=st.lists(st.sampled_from(sorted(bl.FLAGS)), unique=True),
)


@given(
    st.lists(items, max_size=8), st.lists(st.sampled_from(["", "# Heading", "prose line", "- plain bullet"]))
)
def test_rendered_backlogs_round_trip(generated: list[bl.BacklogItem], extra: list[str]) -> None:
    doc = bl.parse("# Backlog\n")
    for line in extra:
        doc.blocks.append(line)
    for item in generated:
        item.id = doc.next_id()
        doc.add(item)
    text = doc.render()
    parsed = bl.parse(text)
    assert parsed.render() == text
    assert [(i.id, i.title, i.acceptance, i.flags) for i in parsed.items] == [
        (i.id, i.title, i.acceptance, i.flags) for i in generated
    ]
