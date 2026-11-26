"""Task selection (plan §7.4, ADR 0016)."""

from __future__ import annotations

from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from surplus.optimizer.selection import Candidate, Mode, project_shares, select_task


def cand(project: str = "a", task: str = "T-001", **kw: object) -> Candidate:
    base = Candidate(
        project_id=project,
        project_priority=2,
        max_share=0.5,
        task_id=task,
        task_priority=1,
        position=0,
        size="S",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def chosen(cands: list[Candidate], room: float = 50, spend: dict[str, float] | None = None) -> str | None:
    c = select_task(cands, room, spend or {}).choice
    return f"{c.project_id}/{c.task_id}" if c else None


def test_order_answered_then_project_then_task_priority_then_position() -> None:
    a1 = cand("a", "T-001", position=0)
    a2 = cand("a", "T-002", position=1)
    assert chosen([a2, a1]) == "a/T-001"
    assert chosen([a1, replace(a2, task_priority=0)]) == "a/T-002"
    b = cand("b", "T-009", project_priority=1, task_priority=3)
    assert chosen([a1, b]) == "b/T-009"
    answered = replace(a2, answered=True, project_priority=5)
    assert chosen([a1, b, answered]) == "a/T-002"


def test_waiting_tasks_are_skipped_with_a_reason() -> None:
    sel = select_task(
        [cand(task="T-001", question_pending=True), cand(task="T-002", awaiting_review=True)], 50, {}
    )
    assert sel.choice is None
    assert [why for _, why in sel.skipped] == [
        "waits for your answer to a question",
        "a run of it waits for your review",
    ]
    assert select_task([cand(active=False)], 50, {}) == select_task([], 50, {})


def test_size_fit_and_split_mode() -> None:
    small, medium, large = cand(task="T-1", size="S"), cand(task="T-2", size="M"), cand(task="T-3", size="L")
    unsized = cand(task="T-4", size=None)
    assert chosen([medium], room=10) is None
    assert chosen([unsized], room=10) is None and chosen([unsized], room=15) == "a/T-4"
    sel = select_task([large], 10, {})
    assert sel.choice is not None and sel.choice.mode is Mode.SPLIT and "smaller tasks" in sel.choice.reason
    assert select_task([large], 4, {}).choice is None
    assert select_task([large], 30, {}).choice.mode is Mode.WORK  # type: ignore[union-attr]
    # Same rank: real work beats splitting.
    assert chosen([replace(large, position=0), replace(small, position=0)], room=10) == "a/T-1"


def test_fair_share_is_work_conserving() -> None:
    a, b = cand("a", project_priority=1), cand("b", project_priority=2)
    assert chosen([a, b], spend={"a": 8, "b": 2}) == "b/T-001"  # a had 80% > 50%
    assert chosen([a], spend={"a": 8, "b": 2}) == "a/T-001"  # alone: may use everything
    assert chosen([a, b], spend={"a": 5, "b": 5}) == "a/T-001"
    tight = [replace(a, max_share=0.3), replace(b, max_share=0.3)]
    assert chosen(tight, spend={"a": 5, "b": 5}) == "a/T-001"  # both over: nobody is starved
    assert project_shares({}) == {} and project_shares({"a": 0}) == {"a": 0.0}


@settings(max_examples=60, deadline=None)
@given(
    share=st.sampled_from([0.3, 0.5, 0.7]),
    costs=st.lists(st.floats(min_value=1, max_value=10), min_size=60, max_size=60),
)
def test_long_run_share_respects_the_cap_while_others_wait(share: float, costs: list[float]) -> None:
    """Two projects that always have work: the favoured one stays near its cap (within one run)."""
    a = cand("a", project_priority=1, max_share=share)
    b = cand("b", project_priority=2, max_share=1.0)
    spend = {"a": 0.0, "b": 0.0}
    for cost in costs:
        c = select_task([a, b], 50, spend).choice
        assert c is not None
        spend[c.project_id] += cost
    total = spend["a"] + spend["b"]
    assert spend["a"] <= share * total + 10 + 1e-9
    assert spend["a"] > 0 and spend["b"] > 0
