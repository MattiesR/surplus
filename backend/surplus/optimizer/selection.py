"""Task selection (plan §7.4) — pure. Which task an automatic run works on (ADR 0016).

1. **Eligible:** the project is active and not over its fair share (below), the task is open, not proposed,
   not blocked, has no question waiting for the user, and no run of it is waiting for review (no second run
   while the user has not looked at the first).
2. **Order:** tasks with a freshly answered question first (they were waiting for the user), then project
   priority (1 = most important), then task priority (P0 first), then backlog order (the user's ordering,
   which stands in for goal order: tasks do not reference goals).
3. **Size fit:** a task needs room: S 5, M 15, L 30 pp of the 5-hour window (unsized = M). A task that does
   not fit is skipped, except an L task in a slot that fits at least an S: it is chosen in *split* mode, and
   the agent proposes smaller tasks instead of starting it.
4. **Fair share** over a rolling 7 days: a project that already had more than `max_share` of all agent spend
   is skipped *while another project has eligible work* (work-conserving: a lone project may use everything).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

SIZE_NEED: Mapping[str | None, float] = {"S": 5.0, "M": 15.0, "L": 30.0, None: 15.0}
SPLIT_MIN = SIZE_NEED["S"]


class Mode(StrEnum):
    WORK = "work"
    SPLIT = "split"  # propose smaller tasks instead of starting this one


@dataclass(frozen=True)
class Candidate:
    project_id: str
    project_priority: int  # 1..5, 1 first
    max_share: float  # 0..1, project.md max_share_of_surplus
    task_id: str
    task_priority: int  # 0..3, P0 first
    position: int  # index in backlog.md
    size: str | None  # "S" | "M" | "L" | None
    answered: bool = False  # a question about this task was answered since its last run
    question_pending: bool = False  # a question about this task waits for the user
    awaiting_review: bool = False  # a run of this task waits for merge/discard
    active: bool = True  # project status active, task open, not proposed, not blocked


@dataclass(frozen=True)
class Choice:
    project_id: str
    task_id: str
    mode: Mode
    reason: str


@dataclass(frozen=True)
class Selection:
    choice: Choice | None
    skipped: tuple[tuple[str, str], ...] = field(default=())  # (project/task, why), for the decision log


def project_shares(spend: Mapping[str, float]) -> dict[str, float]:
    """Share of the agents' 7-day spend per project (0..1)."""
    total = sum(max(0.0, v) for v in spend.values())
    return {p: (max(0.0, v) / total if total > 0 else 0.0) for p, v in spend.items()}


def _fits(c: Candidate, room: float) -> Mode | None:
    need = SIZE_NEED.get(c.size, SIZE_NEED[None])
    if room >= need:
        return Mode.WORK
    if c.size == "L" and room >= SPLIT_MIN:
        return Mode.SPLIT
    return None


def select_task(candidates: Sequence[Candidate], room: float, spend_7d: Mapping[str, float]) -> Selection:
    """`room` = pp of the 5-hour window the run may use (the planner's allowed_now)."""
    skipped: list[tuple[str, str]] = []
    usable: list[tuple[Candidate, Mode]] = []
    for c in candidates:
        name = f"{c.project_id}/{c.task_id}"
        if not c.active:
            continue  # not a candidate at all: done, proposed, blocked or a paused project
        if c.question_pending:
            skipped.append((name, "waits for your answer to a question"))
        elif c.awaiting_review:
            skipped.append((name, "a run of it waits for your review"))
        elif (mode := _fits(c, room)) is None:
            skipped.append((name, f"needs {SIZE_NEED.get(c.size, 15.0):.0f} pp, {room:.1f} available"))
        else:
            usable.append((c, mode))

    shares = project_shares(spend_7d)
    projects = {c.project_id for c, _ in usable}
    over = {
        p
        for p in projects
        if len(projects) > 1
        and shares.get(p, 0.0) > next(c.max_share for c, _ in usable if c.project_id == p)
    }
    if over and over != projects:
        for c, _ in usable:
            if c.project_id in over:
                skipped.append(
                    (
                        f"{c.project_id}/{c.task_id}",
                        f"over its fair share ({shares[c.project_id]:.0%} in 7 days)",
                    )
                )
        usable = [(c, m) for c, m in usable if c.project_id not in over]

    if not usable:
        return Selection(None, tuple(skipped))
    c, mode = min(
        usable,
        key=lambda cm: (
            not cm[0].answered,
            cm[0].project_priority,
            cm[0].task_priority,
            cm[0].position,
            cm[1] is Mode.SPLIT,  # with everything else equal, real work before splitting
        ),
    )
    why = [
        "your answer unblocked it" if c.answered else f"project priority {c.project_priority}",
        f"P{c.task_priority}",
        f"#{c.position + 1} in the backlog",
    ]
    if mode is Mode.SPLIT:
        why.append(f"too large for {room:.1f} pp: the agent proposes smaller tasks")
    return Selection(Choice(c.project_id, c.task_id, mode, ", ".join(why)), tuple(skipped))
