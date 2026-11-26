# ruff: noqa: F811 - the run fixtures (`env`) are imported from test_runner_service
"""Automatic runs (ADR 0017): opt-in, guard rails, selection, budget and split mode, with real runs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_guard_rails import manual_event
from test_runner_service import Env, env, git_identity, hello_agent  # noqa: F401 - fixtures
from typer.testing import CliRunner

from surplus import preferences
from surplus.collector.service import Collector
from surplus.config import Paths, Settings
from surplus.db.models import RunRow
from surplus.dispatcher import Dispatcher
from surplus.optimizer import control
from surplus.projects import backlog as bl
from surplus.projects import repo


@dataclass
class _Plan:
    allowed_now: float


@dataclass
class _Live:
    plan: _Plan | None


class FakePlanner:
    def __init__(self, allowed: float | None) -> None:
        self.allowed = allowed
        self.calls = 0

    def current(self) -> _Live:
        self.calls += 1
        return _Live(_Plan(self.allowed) if self.allowed is not None else None)


def make(e: Env, paths: Paths, allowed: float | None = 20.0) -> tuple[Dispatcher, FakePlanner]:
    planner = FakePlanner(allowed)
    col = Collector(e.sm, paths, Settings(), e.clock, e.bus)
    d = Dispatcher(e.sm, e.clock, e.bus, col, e.svc, e.projects, 1, planner)  # type: ignore[arg-type]
    return d, planner


def turn_on(e: Env) -> None:
    with e.sm.begin() as s:
        preferences.set_auto_runs(s, True)


async def settle(e: Env) -> None:
    for task in list(e.svc._tasks.values()):
        await asyncio.wait_for(task, 30)


async def test_off_by_default(env: Env, paths: Paths) -> None:
    d, planner = make(env, paths)
    decision = await d.loop_tick()
    assert not decision.auto_start and "off" in decision.reason
    assert planner.calls == 0 and env.svc.active_runs() == []
    assert d.status.auto_enabled is False


async def test_starts_the_selected_task_with_the_plan_budget(env: Env, paths: Paths) -> None:
    d, _ = make(env, paths, allowed=20.0)
    turn_on(env)
    env.runtime.agents.append(hello_agent())
    assert (await d.tick()).reason.startswith("20.0 pp")  # a page load decides, but never starts
    assert env.svc.active_runs() == []
    decision = await d.loop_tick()
    assert "started r1 on demo/T-001" in decision.reason
    row = env.svc.get(1)
    assert row.trigger == "auto" and row.mode == "work"
    assert row.cap_five_hour_pct == pytest.approx(10 + 20)  # used now + allowed_now, under the 85 hard stop
    assert row.budget_pct == pytest.approx(20)
    await settle(env)
    assert env.svc.get(1).state == "awaiting_feedback"
    # Reopened while its run still waits for review: it is not started a second time.
    _, h = repo.ProjectFiles(env.folder).read(repo.DocName.BACKLOG)
    env.projects.update_task("demo", "T-001", h, done=False)
    decision = await d.loop_tick()
    assert "nothing to run" in decision.reason and "waits for your review" in decision.reason


@pytest.mark.parametrize("blocker", ["paused", "active", "small"])
async def test_no_start_when_the_guard_rails_say_no(env: Env, paths: Paths, blocker: str) -> None:
    d, planner = make(env, paths, allowed=3.0 if blocker == "small" else 20.0)
    turn_on(env)
    if blocker == "paused":
        with env.sm.begin() as s:
            control.set_pause(s, env.clock.now(), None)
    if blocker == "active":
        manual_event(env)
    decision = await d.loop_tick()
    assert not decision.auto_start and env.svc.active_runs() == []
    assert planner.calls == (1 if blocker == "small" else 0)  # no planning when a start is impossible


async def test_large_task_in_a_small_slot_is_split(env: Env, paths: Paths) -> None:
    _, h = repo.ProjectFiles(env.folder).read(repo.DocName.BACKLOG)
    env.projects.update_task("demo", "T-001", h, done=True)
    _, h = repo.ProjectFiles(env.folder).read(repo.DocName.BACKLOG)
    env.projects.add_task("demo", h, "Big rewrite", priority=1, size=bl.Size.L, acceptance="all of it")
    d, _ = make(env, paths, allowed=10.0)
    turn_on(env)
    env.runtime.agents.append(hello_agent())
    decision = await d.loop_tick()
    assert "smaller tasks" in decision.reason
    row = env.svc.active_runs()[0]
    assert row.mode == "split"
    await settle(env)
    prompt = (env.svc.run_dir(row.id) / "prompt.md").read_text()
    assert "split the task, do not implement it" in prompt


async def test_refused_start_backs_off(env: Env, paths: Paths) -> None:
    d, _ = make(env, paths, allowed=20.0)
    turn_on(env)
    env.runtime.image = False
    first = await d.loop_tick()
    assert "could not start" in first.reason and "runner image" in first.reason
    second = await d.loop_tick()
    assert "nothing to run" in second.reason  # T-001 is in its 30-minute back-off


def test_cli_auto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from surplus.cli import app

    for name in ("DATA", "CONFIG", "CLAUDE"):
        monkeypatch.setenv(f"SURPLUS_{name}_DIR", str(tmp_path / name))
    run = CliRunner()
    assert "off" in run.invoke(app, ["auto"]).output
    assert "ON" in run.invoke(app, ["auto", "on"]).output
    assert "ON" in run.invoke(app, ["auto"]).output
    assert "off" in run.invoke(app, ["auto", "off"]).output
    assert run.invoke(app, ["auto", "maybe"]).exit_code != 0


async def test_a_simulated_stretch_of_the_day(env: Env, paths: Paths) -> None:
    """Minute ticks with a fake clock: start, the user arrives (kill switch), no starts while they work,
    a new start once they have been idle for 10 minutes."""
    from datetime import timedelta

    from test_guard_rails import slow_agent

    _, h = repo.ProjectFiles(env.folder).read(repo.DocName.BACKLOG)
    env.projects.add_task("demo", h, "Second", priority=1, size=bl.Size.S, acceptance="done")
    d, _ = make(env, paths, allowed=20.0)
    turn_on(env)
    env.runtime.line_delay = 0.05
    env.runtime.agents.extend([slow_agent, hello_agent()])

    first = await d.loop_tick()
    assert "started r1" in first.reason
    for _ in range(100):
        if env.svc.get(1).state == "running":
            break
        await asyncio.sleep(0.02)
    env.clock.advance(timedelta(minutes=1))
    manual_event(env)  # the user starts working
    env.clock.advance(timedelta(minutes=1))
    stop = await d.loop_tick()
    assert [s.run_id for s in stop.stops] == [1]
    await settle(env)
    assert env.svc.get(1).exit_reason == "user_active"

    for minute in range(9):  # still active: activity counts for 10 minutes
        env.clock.advance(timedelta(minutes=1))
        assert not (await d.loop_tick()).auto_start, minute
    with env.sm() as session:
        assert session.scalar(select(func.count()).select_from(RunRow)) == 1  # no start while active

    env.clock.advance(timedelta(minutes=2))
    resumed = await d.loop_tick()
    assert "started r2" in resumed.reason
    await settle(env)
    assert env.svc.get(2).trigger == "auto"
