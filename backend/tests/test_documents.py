from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from surplus.projects import documents as d

GOALS = """# Goals

Intro.

## G1. Ship a CLI
Some context.

Acceptance:
- `tool --help` prints usage
- [ ] exits 0 on success

## Make it fast
**Acceptance criteria:**
* runs in < 1 s

```md
## not a heading inside a fence
```
"""


def test_goals_parse_and_round_trip() -> None:
    doc = d.parse_goals(GOALS)
    assert doc.render() == GOALS
    g1, g2 = doc.goals
    assert (g1.id, g1.title) == ("G1", "Ship a CLI")
    assert g1.acceptance == ["`tool --help` prints usage", "exits 0 on success"]
    assert (g2.id, g2.title, g2.acceptance) == ("G2", "Make it fast", ["runs in < 1 s"])


@given(st.text(max_size=300))
def test_goals_round_trip_any_text(text: str) -> None:
    assert d.parse_goals(text).render() == text


def test_render_new_goals() -> None:
    text = d.render_new_goals([("Ship  it", ["works", "is  tested"]), ("Docs", [])])
    goals = d.parse_goals(text).goals
    assert [(g.id, g.title, g.acceptance) for g in goals] == [
        ("G1", "Ship it", ["works", "is tested"]),
        ("G2", "Docs", []),
    ]


def test_decisions_append_only() -> None:
    when = datetime(2026, 11, 26, 3, 12, tzinfo=UTC)
    text = d.append_decision(d.NEW_DECISIONS, when, "Which DB?", "B — SQLite", ref="Q1 · run r42")
    assert text.startswith(d.NEW_DECISIONS)
    text2 = d.append_decision(text, when, "Tabs or spaces?", "Spaces")
    assert text2.startswith(text)
    first, second = d.parse_decisions(text2)
    assert first.heading == "2026-11-26 03:12 UTC · Q1 · run r42"
    assert (first.question, first.answer) == ("Which DB?", "B — SQLite")
    assert second.answer == "Spaces"
