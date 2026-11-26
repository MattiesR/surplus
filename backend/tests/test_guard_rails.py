# ruff: noqa: F811 - the run fixtures (`env`) are imported from test_runner_service
"""Guard rails (plan §7.3): pure decisions, the pause store, and the dispatcher shell with real runs."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from fixtures.fake_runtime import init, limits, report_block, result, text
from sqlalchemy.orm import Session, sessionmaker
from test_runner_service import Env, commit_hello, env, git_identity  # noqa: F401 - fixtures
from typer.testing import CliRunner

from surplus.collector.service import Collector
from surplus.collector.store import insert_events
from surplus.config import Paths, Settings
from surplus.db.models import RunRow
from surplus.dispatcher import Dispatcher
from surplus.domain.models import TokenCounts, UsageEvent, UsageSourceKind
from surplus.optimizer import control
from surplus.optimizer.guard_rails import ActiveRun, FinishedRun, breaker_trips, guard
from surplus.predictor.activity import Activity
from surplus.projects import repo
from surplus.runner.service import PreflightError
from surplus.runner.state import ExitReason

from conftest import T0

IDLE = Activity(False, None, None)
BUSY = Activity(True, "a manual Claude Code session 2 min ago", T0)
RUNS = [ActiveRun(1, "manual"), ActiveRun(2, "auto")]


# ------------------------------------------------------------------ pure decisions


def test_pause_stops_every_run_and_blocks_starts() -> None:
    d = guard(IDLE, "paused until you resume", RUNS, 50.0, True, 1)
    assert [(s.run_id, s.reason) for s in d.stops] == [(1, ExitReason.PAUSED), (2, ExitReason.PAUSED)]
    assert not d.auto_start


def test_kill_switch_stops_automatic_runs_only() -> None:
    d = guard(BUSY, None, RUNS, 50.0, True, 1)
    assert [(s.run_id, s.reason) for s in d.stops] == [(2, ExitReason.USER_ACTIVE)]
    assert not d.auto_start and d.reason.startswith("kill switch")


@pytest.mark.parametrize(
    ("allowed", "fresh", "active", "why"),
    [
        (None, True, [], "no plan yet"),
        (50.0, False, [], "no fresh reset"),
        (50.0, True, [ActiveRun(1, "manual")], "in progress"),
        (2.0, True, [], "needs 5"),
    ],
)
def test_auto_start_needs_room_fresh_resets_and_a_free_slot(
    allowed: float | None, fresh: bool, active: list[ActiveRun], why: str
) -> None:
    d = guard(IDLE, None, active, allowed, fresh, 1)
    assert not d.auto_start and why in d.reason and d.stops == ()
    assert guard(IDLE, None, [], 6.0, True, 1).auto_start
    off = guard(IDLE, None, [], 60.0, True, 1, auto_enabled=False)
    assert not off.auto_start and "off" in off.reason


def test_breaker() -> None:
    fails = [FinishedRun(i, True) for i in (3, 4, 5)]
    assert breaker_trips(fails, None)
    assert not breaker_trips(fails, 5)  # resumed after this trip: not again until a new failure
    assert breaker_trips([*fails, FinishedRun(6, True)], 5)
    assert not breaker_trips([*fails, FinishedRun(6, False)], None)  # a success resets the count
    assert not breaker_trips(fails[:2], None)


# ------------------------------------------------------------------ pause store and CLI


def test_pause_roundtrip(sm: sessionmaker[Session]) -> None:
    with sm.begin() as s:
        state = control.set_pause(s, T0, timedelta(hours=4), "  demo day  ")
    assert state.reason == "demo day" and "until" in state.describe()
    with sm() as s:
        assert control.get_pause(s, T0 + timedelta(hours=3, minutes=59)) is not None
        assert control.get_pause(s, T0 + timedelta(hours=4)) is None  # expired = not paused
    with sm.begin() as s:
        control.set_pause(s, T0, None)
    with sm() as s:
        p = control.get_pause(s, T0 + timedelta(days=400))
        assert p is not None and p.indefinite
    with sm.begin() as s:
        assert control.clear_pause(s)
        assert not control.clear_pause(s)


@pytest.mark.parametrize("bad", ["", "4", "4x", "-1h", "0m", "31d", "4 hours"])
def test_parse_duration_rejects(bad: str) -> None:
    with pytest.raises(ValueError, match=r"duration|between"):
        control.parse_duration(bad)


def test_parse_duration() -> None:
    assert control.parse_duration("90m") == timedelta(minutes=90)
    assert control.parse_duration("1.5h") == timedelta(hours=1, minutes=30)
    assert control.parse_duration("2d") == timedelta(days=2)


def test_cli_pause_and_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from surplus.cli import app

    for name, sub in (("DATA", "d"), ("CONFIG", "c"), ("CLAUDE", "cl"), ("PROJECTS_ROOT", "p")):
        monkeypatch.setenv(
            f"SURPLUS_{name}_DIR" if name != "PROJECTS_ROOT" else "SURPLUS_PROJECTS_ROOT", str(tmp_path / sub)
        )

    out = CliRunner().invoke(app, ["pause", "4h", "--reason", "deadline"])
    assert out.exit_code == 0 and "paused until" in out.output and "deadline" in out.output, out.output
    assert CliRunner().invoke(app, ["pause", "soon"]).exit_code != 0
    assert "Resumed" in CliRunner().invoke(app, ["resume"]).output
    assert "not paused" in CliRunner().invoke(app, ["resume"]).output


# ------------------------------------------------------------------ the dispatcher with real runs


def slow_agent(ws: Path, argv: list[str], prompt: str, secrets: dict[str, str]) -> list[str]:
    commit_hello(ws)
    return [init(), *[text(f"step {i}") for i in range(40)], limits(12, 6), result(report_block())]


def dispatcher(e: Env, paths: Paths) -> Dispatcher:
    col = Collector(e.sm, paths, Settings(), e.clock, e.bus)
    return Dispatcher(e.sm, e.clock, e.bus, col, e.svc, e.projects, 1)


async def wait_running(e: Env, run_id: int) -> None:
    for _ in range(200):
        if e.svc.get(run_id).state == "running":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("run did not start")


def manual_event(e: Env, at_offset: timedelta = timedelta(0)) -> None:
    with e.sm.begin() as s:
        insert_events(
            s,
            [
                UsageEvent(
                    message_id="manual-1",
                    ts=e.clock.now() + at_offset,
                    source=UsageSourceKind.CC_LOG,
                    session_id="mine",
                    model="claude-sonnet-5-5",
                    tokens=TokenCounts(input=10, output=10),
                )
            ],
        )


async def test_kill_switch_wraps_up_an_automatic_run_on_the_next_tick(env: Env, paths: Paths) -> None:
    """DoD: the kill switch pauses within 60 s of manual activity. The tick runs right after each
    ingestion (every `tick_seconds`, 60 by default), and one tick is enough."""
    assert Settings().tick_seconds <= 60
    d = dispatcher(env, paths)
    env.runtime.line_delay = 0.05
    env.runtime.agents.append(slow_agent)
    row = await env.svc.start("demo", "T-001", trigger="auto")
    await wait_running(env, row.id)
    assert (await d.tick()).stops == ()  # idle user: nothing happens
    manual_event(env)
    env.clock.advance(timedelta(seconds=59))  # the next tick, at most a minute after the activity
    decision = await d.tick()
    assert [s.run_id for s in decision.stops] == [row.id]
    await asyncio.wait_for(env.svc._tasks[row.id], 30)
    done = env.svc.get(row.id)
    assert done.exit_reason == "user_active" and done.state == "awaiting_feedback"  # work kept, reported
    assert env.svc.get(row.id).trigger == "auto"
    assert d.status.activity is not None and d.status.activity.active


async def test_manual_runs_keep_going_while_the_user_is_active(env: Env, paths: Paths) -> None:
    d = dispatcher(env, paths)
    env.runtime.line_delay = 0.02
    env.runtime.agents.append(slow_agent)
    row = await env.svc.start("demo", "T-001")
    await wait_running(env, row.id)
    manual_event(env)
    assert (await d.tick()).stops == ()
    await asyncio.wait_for(env.svc._tasks[row.id], 30)
    assert env.svc.get(row.id).exit_reason == "completed"


async def test_pause_stops_runs_and_blocks_new_ones_until_resumed(env: Env, paths: Paths) -> None:
    d = dispatcher(env, paths)
    env.runtime.line_delay = 0.05
    env.runtime.agents.append(slow_agent)
    row = await env.svc.start("demo", "T-001")
    await wait_running(env, row.id)
    await d.pause(timedelta(hours=3), "meeting")
    await asyncio.wait_for(env.svc._tasks[row.id], 30)
    done = env.svc.get(row.id)
    assert done.exit_reason == "paused" and done.state == "awaiting_feedback"
    assert any(p.startswith("Surplus is paused") for p in env.svc.preflight("demo", "T-001").problems)
    with pytest.raises(PreflightError, match="resume first"):
        await env.svc.start("demo", "T-001")
    assert await d.resume()
    assert not any("paused" in p for p in env.svc.preflight("demo", "T-001").problems)
    # an expired pause needs no resume
    await d.pause(timedelta(minutes=30))
    env.clock.advance(timedelta(minutes=31))
    assert not any("paused" in p for p in env.svc.preflight("demo", "T-001").problems)


async def test_circuit_breaker_pauses_the_project_after_three_failures(env: Env, paths: Paths) -> None:
    d = dispatcher(env, paths)
    with env.sm.begin() as s:
        for _ in range(3):
            s.add(
                RunRow(
                    project_id="demo",
                    task_id="T-001",
                    state="failed",
                    model="sonnet",
                    max_turns=10,
                    created_at=T0,
                    updated_at=T0,
                )
            )
    await d.tick()
    assert env.projects.snapshot("demo").meta.status is repo.ProjectStatus.PAUSED  # type: ignore[union-attr]
    assert d.status.tripped == ["demo"]
    # The user resumes the project: the same failures do not trip it again.
    _, h = repo.ProjectFiles(env.folder).read(repo.DocName.PROJECT)
    env.projects.update_settings("demo", {"status": "active"}, h)
    await d.tick()
    assert env.projects.snapshot("demo").meta.status is repo.ProjectStatus.ACTIVE  # type: ignore[union-attr]
