"""Run state machine (plan §7.5) — pure.

queued → preparing → running → wrapping_up → verifying → reporting → awaiting_feedback → done

- `wrapping_up` always follows `running`: the agent finished or was asked to stop, and Surplus commits
  leftover changes in the run's clone and fetches the branch. A stop for budget or by the user records an
  `exit_reason` and still verifies and reports, so work in progress is never lost.
- Side exits `budget_exhausted` and `paused_by_user` end a run that had not started working yet.
- `failed` and `cancelled` can end any run that is not yet waiting for feedback.
- `awaiting_feedback → done` happens when the user merges or discards the branch.
"""

from __future__ import annotations

import itertools
from enum import StrEnum


class RunState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    WRAPPING_UP = "wrapping_up"
    VERIFYING = "verifying"
    REPORTING = "reporting"
    AWAITING_FEEDBACK = "awaiting_feedback"
    DONE = "done"
    PAUSED_BY_USER = "paused_by_user"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExitReason(StrEnum):
    COMPLETED = "completed"
    MAX_TURNS = "max_turns"
    TIMEOUT = "timeout"
    BUDGET = "budget_exhausted"
    USER_STOP = "stopped_by_user"
    AGENT_ERROR = "agent_error"
    INTERRUPTED = "interrupted"  # Surplus restarted mid-run
    PREFLIGHT = "preflight"
    INTERNAL_ERROR = "internal_error"
    USER_ACTIVE = "user_active"  # kill switch: the user started working (plan §7.3)
    PAUSED = "paused"  # global pause from the UI, CLI or phone


S = RunState
MAIN_PATH: tuple[RunState, ...] = (
    S.QUEUED,
    S.PREPARING,
    S.RUNNING,
    S.WRAPPING_UP,
    S.VERIFYING,
    S.REPORTING,
    S.AWAITING_FEEDBACK,
    S.DONE,
)
TERMINAL = frozenset({S.DONE, S.PAUSED_BY_USER, S.BUDGET_EXHAUSTED, S.FAILED, S.CANCELLED})
# States in which a container may be running and an asyncio task is driving the run.
ACTIVE = frozenset({S.QUEUED, S.PREPARING, S.RUNNING, S.WRAPPING_UP, S.VERIFYING, S.REPORTING})


def _allowed() -> dict[RunState, frozenset[RunState]]:
    table: dict[RunState, set[RunState]] = {s: set() for s in RunState}
    for a, b in itertools.pairwise(MAIN_PATH):
        table[a].add(b)
    for s in ACTIVE:
        table[s] |= {S.FAILED, S.CANCELLED}
    for s in (S.QUEUED, S.PREPARING):
        table[s] |= {S.BUDGET_EXHAUSTED, S.PAUSED_BY_USER}
    return {k: frozenset(v) for k, v in table.items()}


ALLOWED: dict[RunState, frozenset[RunState]] = _allowed()


class TransitionError(ValueError):
    pass


def transition(current: RunState, target: RunState) -> RunState:
    """Return `target` if the move is legal, else raise. Never silently ignores a bad move."""
    if target not in ALLOWED[current]:
        raise TransitionError(f"illegal run transition {current} → {target}")
    return target


def recover(state: RunState) -> RunState | None:
    """State to record for a run found in `state` after a restart (None = leave as is)."""
    return S.FAILED if state in ACTIVE else None
