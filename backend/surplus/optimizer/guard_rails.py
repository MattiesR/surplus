"""Dispatcher guard rails (plan §7.3) — pure. The hard rules, enforced in code and not in the plan.

Every tick (right after log ingestion, so within a minute of the user's activity):
- **global pause** → every active run wraps up, nothing starts;
- **kill switch**: the user is active → runs that *Surplus* started (`trigger == "auto"`) wrap up, and no
  automatic run starts. Runs the user started by hand keep going (the user chose them; ADR 0014) but the
  start panel warns;
- automatic starts additionally need `allowed_now >= min_run_pct`, fresh reset times and a free slot.
Hard stops at the 5-hour and weekly caps are enforced live by the runner on every reading.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from surplus.predictor.activity import Activity
from surplus.runner.state import ExitReason

MIN_RUN_PCT = 5.0  # pp5: an automatic run needs room for a task (ADR 0013)


@dataclass(frozen=True)
class ActiveRun:
    id: int
    trigger: str  # "manual" | "auto"


@dataclass(frozen=True)
class Stop:
    run_id: int
    reason: ExitReason
    detail: str


@dataclass(frozen=True)
class GuardDecision:
    stops: tuple[Stop, ...]
    auto_start: bool
    reason: str  # why automatic starts are (not) allowed right now


def guard(
    activity: Activity,
    pause_text: str | None,  # PauseState.describe() when a pause is in force
    active: Sequence[ActiveRun],
    allowed_now: float | None,  # None: no plan (missing or stale readings)
    resets_fresh: bool,
    max_concurrent: int,
    min_run_pct: float = MIN_RUN_PCT,
) -> GuardDecision:
    if pause_text is not None:
        stops = tuple(Stop(r.id, ExitReason.PAUSED, pause_text) for r in active)
        return GuardDecision(stops, False, pause_text)
    if activity.active:
        detail = activity.reason or "you are active"
        stops = tuple(Stop(r.id, ExitReason.USER_ACTIVE, detail) for r in active if r.trigger == "auto")
        return GuardDecision(stops, False, f"kill switch: {detail}")
    if not resets_fresh:
        return GuardDecision((), False, "no fresh reset times from Claude: waiting for a probe")
    if allowed_now is None:
        return GuardDecision((), False, "no plan to start from: automatic runs arrive in Phase 5")
    if len(active) >= max_concurrent:
        return GuardDecision((), False, "a run is in progress")
    if allowed_now < min_run_pct:
        return GuardDecision((), False, f"{allowed_now:.1f} pp allowed now; a run needs {min_run_pct:.0f}")
    return GuardDecision((), True, f"{allowed_now:.1f} pp of the 5-hour window allowed now")


BREAKER_FAILURES = 3  # plan §7.3: after 3 failed runs on a project, pause the project


@dataclass(frozen=True)
class FinishedRun:
    id: int
    failed: bool


def breaker_trips(
    recent: Sequence[FinishedRun], last_tripped_run: int | None, failures: int = BREAKER_FAILURES
) -> bool:
    """True when the project's `failures` most recent finished runs all failed and the newest of them is
    newer than the run that tripped the breaker last time (so resuming the project does not re-trip it
    until another run fails)."""
    newest = sorted(recent, key=lambda r: r.id, reverse=True)[:failures]
    if len(newest) < failures or not all(r.failed for r in newest):
        return False
    return last_tripped_run is None or newest[0].id > last_tripped_run
