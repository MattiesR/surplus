"""Run pipeline end to end with real git and a FakeRuntime (no Podman, no quota)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from fixtures.fake_runtime import FakeRuntime, init, limits, report_block, result, text, tool
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import FakeClock
from surplus.collector.store import insert_readings
from surplus.config import Paths, Settings
from surplus.db.models import LimitReadingRow, RunRow
from surplus.domain.events import EventBus
from surplus.domain.models import LimitReading, ReadingSource
from surplus.projects import backlog as bl
from surplus.projects import repo
from surplus.projects.registry import ProjectRegistry
from surplus.projects.service import ProjectService
from surplus.runner.container import TOKEN_ENV
from surplus.runner.service import PreflightError, RunActionError, RunService

from conftest import T0

TOKEN = "test-token-value"


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True, timeout=30
    ).stdout


@dataclass
class Env:
    svc: RunService
    runtime: FakeRuntime
    projects: ProjectService
    folder: Path
    sm: sessionmaker[Session]
    clock: FakeClock
    bus: EventBus


@pytest.fixture(autouse=True)
def git_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        "[user]\n\tname = Test User\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))


def add_readings(sm: sessionmaker[Session], clock: FakeClock, five: float = 10.0, week: float = 5.0) -> None:
    now = clock.now()
    with sm.begin() as session:
        insert_readings(
            session,
            [
                LimitReading(
                    ts=now,
                    bucket="five_hour",
                    used_pct=five,
                    resets_at=now + timedelta(hours=2),
                    source=ReadingSource.PROBE,
                ),
                LimitReading(
                    ts=now,
                    bucket="seven_day",
                    used_pct=week,
                    resets_at=now + timedelta(days=3),
                    source=ReadingSource.PROBE,
                ),
            ],
        )


@pytest.fixture
def env(paths: Paths, sm: sessionmaker[Session], clock: FakeClock, tmp_path: Path) -> Env:
    root = tmp_path / "projects"
    root.mkdir()
    bus = EventBus()
    registry = ProjectRegistry(sm, root, clock, bus)
    projects = ProjectService(registry, home=tmp_path / "home", protected=(paths.data_dir, paths.config_dir))
    folder = root / "demo"
    repo.create(
        folder, repo.NewProject(name="Demo", test_command="test -f hello.txt"), git_init_if_needed=True
    )
    (folder / "README.md").write_text("# Demo\n")
    git(folder, "add", "README.md")
    git(folder, "commit", "-q", "-m", "init")
    registry.scan_all()
    _, h = repo.ProjectFiles(folder).read(repo.DocName.BACKLOG)
    projects.add_task("demo", h, "Say hello", priority=1, size=bl.Size.S, acceptance="hello.txt exists")
    add_readings(sm, clock)
    runtime = FakeRuntime()
    svc = RunService(
        sm,
        paths,
        Settings(),
        clock,
        bus,
        registry,
        projects,
        runtime,
        token=lambda: TOKEN,
        claude_bin=lambda: Path("/usr/bin/true"),
    )
    return Env(svc, runtime, projects, folder, sm, clock, bus)


def commit_hello(ws: Path, msg: str = "feat: say hello") -> None:
    (ws / "hello.txt").write_text("hello\n")
    git(ws, "add", "-A")
    git(ws, "-c", "user.name=Agent", "-c", "user.email=a@a.invalid", "commit", "-q", "-m", msg)


def hello_agent(final: str | None = None, five: float = 12.0):  # type: ignore[no-untyped-def]
    def agent(ws: Path, argv: list[str], prompt: str, secrets: dict[str, str]) -> list[str]:
        commit_hello(ws)
        return [
            init(),
            text("Looking around"),
            tool("Bash", command="ls"),
            limits(five, 6),
            result(final if final is not None else report_block()),
        ]

    return agent


async def run_to_end(e: Env, task: str = "T-001") -> RunRow:
    row = await e.svc.start("demo", task)
    await asyncio.wait_for(e.svc._tasks[row.id], 30)
    return e.svc.get(row.id)


async def test_happy_path(env: Env) -> None:
    env.runtime.agents.append(hello_agent())
    run = await run_to_end(env)

    assert run.state == "awaiting_feedback", run.error
    assert run.exit_reason == "completed"
    assert run.verified_tests == "pass"
    assert run.report_source == "agent"
    assert run.commits == 1
    assert run.branch == "surplus/T-001-say-hello"
    assert run.spent_pct_est == pytest.approx(2.0)  # 10% before → 12% after
    # The branch is in the project; the user's checkout is untouched.
    assert git(env.folder, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert not (env.folder / "hello.txt").exists()
    assert git(env.folder, "log", "--format=%s", "-1", run.branch).strip() == "feat: say hello"
    # Backlog: task ticked (tests passed), suggestion added as proposed.
    doc = bl.parse((env.folder / ".surplus/backlog.md").read_text())
    assert doc.get("T-001").done
    assert doc.get("T-002").proposed and doc.get("T-002").title == "Add a goodbye file"
    # Human-readable report in the project.
    assert run.report_path is not None
    md = Path(run.report_path).read_text()
    assert "## Questions" in md and "Q1" in md and "**pass**" in md
    # A git bundle snapshot was taken before the run.
    assert (env.svc.run_dir(run.id) / "snapshot.bundle").is_file()
    # Live log has the agent's steps and Surplus's own steps, in order.
    kinds = [e["kind"] for e in env.svc.log_entries(run.id)]
    assert {"system", "text", "tool", "limit", "result", "surplus"} <= set(kinds)
    seqs = [e["seq"] for e in env.svc.log_entries(run.id)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # Stream readings were stored as authoritative readings.
    with env.sm() as s:
        assert (
            s.scalars(select(LimitReadingRow).where(LimitReadingRow.source == "stream")).first() is not None
        )
    # The container was removed at the end.
    assert env.svc.container_name(run.id) in env.runtime.removed


async def test_token_only_reaches_the_cli_exec(env: Env) -> None:
    env.runtime.agents.append(hello_agent())
    run = await run_to_end(env)
    spec = env.runtime.specs[env.svc.container_name(run.id)]
    assert TOKEN not in json.dumps(dict(spec.env))
    streams = [c.args for c in env.runtime.calls if c.kind == "exec_stream"]
    assert streams and all(s["secrets"] == {TOKEN_ENV: TOKEN} for s in streams)
    execs = [c.args for c in env.runtime.calls if c.kind == "exec"]
    assert TOKEN not in json.dumps(execs)  # test command and git never see it
    assert TOKEN not in (env.svc.run_dir(run.id) / "log.jsonl").read_text()
    argv = streams[0]["argv"]
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


async def test_prompt_contains_task_and_rules(env: Env) -> None:
    seen: dict[str, str] = {}

    def agent(ws: Path, argv: list[str], prompt: str, secrets: dict[str, str]) -> list[str]:
        seen["prompt"] = prompt
        seen["rules"] = argv[argv.index("--append-system-prompt") + 1]
        return [init(), result(report_block(tests="none"))]

    env.runtime.agents.append(agent)
    await run_to_end(env)
    assert "T-001 — Say hello" in seen["prompt"] and "hello.txt exists" in seen["prompt"]
    assert "surplus/T-001-say-hello" in seen["rules"] and "test -f hello.txt" in seen["rules"]
    assert "Never push" in seen["rules"]


async def test_invalid_report_is_repaired_once(env: Env) -> None:
    env.runtime.agents.append(hello_agent(final="I did it, no JSON though."))
    env.runtime.agents.append(lambda ws, argv, p, s: [result(report_block(summary="Repaired."))])
    run = await run_to_end(env)
    assert run.report_source == "repaired"
    repair = [c.args["argv"] for c in env.runtime.calls if c.kind == "exec_stream"][1]
    assert "--resume" in repair and "--max-turns" in repair and repair[repair.index("--max-turns") + 1] == "1"


async def test_fallback_report_when_repair_fails(env: Env) -> None:
    env.runtime.agents.append(hello_agent(final="nothing useful"))
    env.runtime.agents.append(lambda ws, argv, p, s: [result("still no json")])
    run = await run_to_end(env)
    assert run.report_source == "fallback"
    assert run.report_json and "generated from the branch" in run.report_json


async def test_budget_hard_stop_wraps_up_and_still_reports(env: Env) -> None:
    env.runtime.agents.append(hello_agent(final="no json", five=90.0))
    run = await run_to_end(env)
    assert run.exit_reason == "budget_exhausted"
    assert env.runtime.signals == [env.svc.container_name(run.id)]
    assert run.state == "awaiting_feedback" and run.commits == 1  # work is kept
    assert run.report_source == "fallback"  # no repair turn after a budget stop
    assert sum(1 for c in env.runtime.calls if c.kind == "exec_stream") == 1


async def test_leftover_changes_are_committed(env: Env) -> None:
    def agent(ws: Path, argv: list[str], prompt: str, secrets: dict[str, str]) -> list[str]:
        (ws / "hello.txt").write_text("uncommitted\n")
        return [init(), result(report_block())]

    env.runtime.agents.append(agent)
    run = await run_to_end(env)
    assert run.commits == 1
    msg = git(env.folder, "log", "--format=%s", "-1", run.branch or "")
    assert "uncommitted work left by the agent" in msg


async def test_agent_destroying_its_git_dir_cannot_hurt_the_project(env: Env) -> None:
    before = git(env.folder, "rev-parse", "HEAD")

    def agent(ws: Path, argv: list[str], prompt: str, secrets: dict[str, str]) -> list[str]:
        subprocess.run(["rm", "-rf", str(ws / ".git")], check=True)
        return [init(), result(report_block())]

    env.runtime.agents.append(agent)
    run = await run_to_end(env)
    assert run.state == "awaiting_feedback"
    assert run.commits == 0
    assert git(env.folder, "rev-parse", "HEAD") == before
    assert git(env.folder, "fsck", "--no-progress") == ""


async def test_no_commits_means_no_branch_and_no_tick(env: Env) -> None:
    env.runtime.agents.append(lambda ws, argv, p, s: [init(), result(report_block())])
    run = await run_to_end(env)
    assert run.commits == 0 and run.verified_tests == "fail"
    assert not git(env.folder, "branch", "--list", "surplus/*").strip()
    assert not bl.parse((env.folder / ".surplus/backlog.md").read_text()).get("T-001").done
    with pytest.raises(RunActionError):
        await env.svc.merge(run.id)
    assert (await env.svc.discard(run.id)).resolution == "discarded"


async def test_merge_and_discard(env: Env) -> None:
    env.runtime.agents.append(hello_agent())
    run = await run_to_end(env)
    merged = await env.svc.merge(run.id)
    assert merged.state == "done" and merged.resolution == "merged"
    assert (env.folder / "hello.txt").read_text() == "hello\n"
    assert "Surplus run r1" in git(env.folder, "log", "--format=%s", "-1")
    assert not (env.svc.run_dir(run.id) / "repo").exists()  # clone cleaned up


async def test_merge_refuses_a_dirty_tree(env: Env) -> None:
    env.runtime.agents.append(hello_agent())
    run = await run_to_end(env)
    (env.folder / "README.md").write_text("local edit\n")
    with pytest.raises(RunActionError, match="uncommitted"):
        await env.svc.merge(run.id)
    assert env.svc.get(run.id).state == "awaiting_feedback"


async def test_rerun_gets_a_unique_branch(env: Env) -> None:
    env.runtime.agents.append(hello_agent())
    first = await run_to_end(env)
    # Reopen the task so it can run again.
    _, h = repo.ProjectFiles(env.folder).read(repo.DocName.BACKLOG)
    env.projects.update_task("demo", "T-001", h, done=False)
    env.runtime.agents.append(hello_agent())
    second = await run_to_end(env)
    assert first.branch == "surplus/T-001-say-hello"
    assert second.branch == "surplus/T-001-say-hello-2"


def test_preflight_problems(env: Env) -> None:
    pf = env.svc.preflight("demo", "T-001")
    assert pf.ok, pf.problems
    assert pf.budget_pct == pytest.approx(75.0)  # 85% hard stop - 10% used

    assert "no task T-099" in " ".join(env.svc.preflight("demo", "T-099").problems)
    env.clock.advance(timedelta(hours=2))
    assert any("fresh reset" in p for p in env.svc.preflight("demo", "T-001").problems)
    add_readings(env.sm, env.clock, five=88.0)
    assert any("five_hour is at 88%" in p for p in env.svc.preflight("demo", "T-001").problems)


def test_preflight_needs_token_and_commits(env: Env, tmp_path: Path) -> None:
    env.svc._token = lambda: None
    assert any("setup-token" in p for p in env.svc.preflight("demo", "T-001").problems)
    (env.folder / "README.md").write_text("changed\n")
    assert any("uncommitted" in w for w in env.svc.preflight("demo", "T-001").warnings)


async def test_start_refuses_when_preflight_fails(env: Env) -> None:
    env.clock.advance(timedelta(minutes=1))  # readings are unique per (bucket, ts, source)
    add_readings(env.sm, env.clock, five=95.0)
    with pytest.raises(PreflightError):
        await env.svc.start("demo", "T-001")
    assert env.svc.list_runs() == []


async def test_one_run_at_a_time(env: Env) -> None:
    gate = asyncio.Event()
    env.runtime.line_delay = 0.05
    env.runtime.agents.append(hello_agent())
    row = await env.svc.start("demo", "T-001")
    with pytest.raises(PreflightError, match="another run"):
        await env.svc.start("demo", "T-001")
    gate.set()
    await asyncio.wait_for(env.svc._tasks[row.id], 30)


async def test_user_stop_and_cancel(env: Env) -> None:
    env.runtime.line_delay = 0.2
    env.runtime.agents.append(hello_agent())
    row = await env.svc.start("demo", "T-001")
    for _ in range(100):
        if env.svc.get(row.id).state == "running":
            break
        await asyncio.sleep(0.02)
    await env.svc.stop(row.id)
    await asyncio.wait_for(env.svc._tasks[row.id], 30)
    done = env.svc.get(row.id)
    assert done.exit_reason == "stopped_by_user" and done.state == "awaiting_feedback"

    _, h = repo.ProjectFiles(env.folder).read(repo.DocName.BACKLOG)
    env.projects.add_task("demo", h, "Second", priority=2, size=None, acceptance=None)
    env.runtime.agents.append(hello_agent())
    row2 = await env.svc.start("demo", "T-003")
    await asyncio.sleep(0.1)
    await env.svc.cancel(row2.id)
    assert env.svc.get(row2.id).state == "cancelled"


async def test_recover_marks_interrupted_runs(env: Env) -> None:
    now = env.clock.now()
    with env.sm.begin() as s:
        s.add(
            RunRow(
                project_id="demo",
                task_id="T-001",
                state="running",
                model="sonnet",
                max_turns=5,
                commits=0,
                base_dirty=False,
                created_at=now,
                updated_at=now,
                container="c1",
            )
        )
    await env.svc.recover()
    row = env.svc.list_runs()[0]
    assert row.state == "failed" and row.exit_reason == "interrupted"
    assert "c1" in env.runtime.removed


async def test_agent_usage_is_ingested_as_agent(env: Env) -> None:
    from surplus.db.models import UsageEventRow

    from conftest import assistant_line

    def agent(ws: Path, argv: list[str], prompt: str, secrets: dict[str, str]) -> list[str]:
        cfg = env.runtime.specs[next(iter(env.runtime.specs))].claude_config
        log_dir = cfg / "projects" / "-workspace"
        log_dir.mkdir(parents=True)
        (log_dir / "s.jsonl").write_text(assistant_line("msg_agent_1") + "\n")
        return [init(), result(report_block(tests="none"))]

    env.runtime.agents.append(agent)
    run = await run_to_end(env)
    with env.sm() as s:
        ev = s.scalars(select(UsageEventRow).where(UsageEventRow.message_id == "msg_agent_1")).one()
    assert ev.is_agent and ev.run_id == f"r{run.id}" and ev.project_id == "demo" and ev.source == "agent"
    _ = T0


# ---------------------------------------------------------------- project overrides and continuation (ADR 0008)


def set_runner(env: Env, **changes: object) -> None:
    _, h = repo.ProjectFiles(env.folder).read(repo.DocName.PROJECT)
    env.projects.update_settings("demo", changes, h)  # type: ignore[arg-type]


def more_agent(seen: dict[str, object]):  # type: ignore[no-untyped-def]
    """A continuation: checks it is on the parent's branch with the parent's commit, then adds one."""

    def agent(ws: Path, argv: list[str], prompt: str, secrets: dict[str, str]) -> list[str]:
        seen["argv"], seen["prompt"] = argv, prompt
        seen["branch"] = git(ws, "rev-parse", "--abbrev-ref", "HEAD").strip()
        seen["hello"] = (ws / "hello.txt").exists()
        (ws / "more.txt").write_text("more\n")
        git(ws, "add", "-A")
        git(ws, "-c", "user.name=Agent", "-c", "user.email=a@a.invalid", "commit", "-q", "-m", "feat: more")
        return [init(), limits(93, 7), result(report_block())]

    return agent


async def test_project_overrides_set_effort_caps_and_turns(env: Env) -> None:
    set_runner(env, model="opus", effort="high", max_five_hour_pct=90, max_turns=80, run_timeout_minutes=90)
    pf = env.svc.preflight("demo", "T-001")
    assert (pf.model, pf.effort, pf.max_turns, pf.cap_five_hour_pct, pf.timeout_minutes) == (
        "opus",
        "high",
        80,
        90,
        90,
    )
    assert pf.budget_pct == pytest.approx(80.0)  # 90% cap minus 10% used
    env.runtime.agents.append(hello_agent(five=88.0))  # above the global 85%, below this project's 90%
    run = await run_to_end(env)
    argv = next(c.args for c in env.runtime.calls if c.kind == "exec_stream")["argv"]
    assert argv[argv.index("--effort") + 1] == "high" and argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--max-turns") + 1] == "80"
    assert run.exit_reason == "completed" and not env.runtime.signals
    assert (run.effort, run.cap_five_hour_pct, run.timeout_minutes) == ("high", 90, 90)


async def test_no_effort_flag_by_default(env: Env) -> None:
    env.runtime.agents.append(hello_agent())
    await run_to_end(env)
    argv = next(c.args for c in env.runtime.calls if c.kind == "exec_stream")["argv"]
    assert "--effort" not in argv


async def test_capped_run_asks_and_continues_on_the_same_branch(env: Env) -> None:
    set_runner(env, max_five_hour_pct=90)
    env.runtime.agents.append(hello_agent(final="no json", five=91.0))
    first = await run_to_end(env)
    assert first.exit_reason == "budget_exhausted" and first.state == "awaiting_feedback"
    from surplus.runner.service import can_continue

    assert can_continue(first)
    first_head = git(env.folder, "rev-parse", first.branch or "").strip()

    seen: dict[str, object] = {}
    env.runtime.agents.append(more_agent(seen))
    child = await env.svc.continue_run(first.id, 95)
    await asyncio.wait_for(env.svc._tasks[child.id], 30)
    child = env.svc.get(child.id)
    parent = env.svc.get(first.id)

    assert (parent.state, parent.resolution) == ("done", "continued")
    assert env.svc.continuation_of(first.id) == child.id
    assert child.parent_run_id == first.id and child.cap_five_hour_pct == 95
    argv = seen["argv"]
    assert isinstance(argv, list) and argv[argv.index("--resume") + 1] == "sess-1"
    assert "allowed you to continue" in str(seen["prompt"]) and "95%" in str(seen["prompt"])
    assert seen["branch"] == first.branch and seen["hello"] is True
    # The continuation ran to the end (93% < its 95% cap) and moved the same branch forward.
    assert child.state == "awaiting_feedback" and child.exit_reason == "completed", child.error
    assert child.branch == first.branch and child.base_sha == first.base_sha
    assert child.commits == 2  # counted from the first run's base
    log = git(env.folder, "log", "--format=%s", child.branch or "").splitlines()
    assert log[:2] == ["feat: more", "feat: say hello"]
    assert git(env.folder, "merge-base", "--is-ancestor", first_head, child.branch or "") == ""
    # The session was carried over, and the continuation can be merged like any run.
    assert (env.svc.run_dir(child.id) / "claude").is_dir()
    merged = await env.svc.merge(child.id)
    assert merged.resolution == "merged" and (env.folder / "more.txt").exists()


async def test_continue_needs_a_higher_cap_and_a_stopped_run(env: Env) -> None:
    env.runtime.agents.append(hello_agent(final="no json", five=86.0))
    capped = await run_to_end(env)
    assert capped.exit_reason == "budget_exhausted"
    with pytest.raises(PreflightError, match="must be above the current 86%"):
        await env.svc.continue_run(capped.id, 86)
    await env.svc.discard(capped.id)
    with pytest.raises(RunActionError, match="cannot be continued"):
        await env.svc.continue_run(capped.id, 95)

    _, h = repo.ProjectFiles(env.folder).read(repo.DocName.BACKLOG)
    env.projects.add_task("demo", h, "Another", priority=1, size=bl.Size.S, acceptance="")
    env.clock.advance(timedelta(minutes=1))
    add_readings(env.sm, env.clock)
    env.runtime.agents.append(hello_agent())
    another = next(
        t for t in bl.parse((env.folder / ".surplus/backlog.md").read_text()).items if t.title == "Another"
    )
    done = await run_to_end(env, another.id)
    assert done.exit_reason == "completed"
    with pytest.raises(RunActionError, match="cannot be continued"):
        await env.svc.continue_run(done.id, 95)


def test_fetch_update_only_fast_forwards(tmp_path: Path) -> None:
    from surplus.runner import gitops

    project = tmp_path / "p"
    project.mkdir()
    git(project, "init", "-q", "-b", "main")
    (project / "a").write_text("a")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "a")
    git(project, "branch", "feature")
    clone = tmp_path / "c"
    gitops.prepare_clone(project, clone, "feature", existing=True)
    (clone / "b").write_text("b")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "b")
    assert gitops.fetch_branch(project, clone, "feature", update=True)  # fast-forward: fine
    git(clone, "reset", "-q", "--hard", "HEAD~1")
    (clone / "c").write_text("c")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "rewritten history")
    with pytest.raises(gitops.GitError):
        gitops.fetch_branch(project, clone, "feature", update=True)  # non-fast-forward: refused
    assert git(project, "log", "--format=%s", "-1", "feature").strip() == "b"


def test_clone_problem_is_reported_by_preflight(env: Env, tmp_path: Path) -> None:
    from surplus.runner import gitops

    assert gitops.clone_problem(env.folder) is None
    # The "dubious ownership" branch needs a repo owned by another uid, which a test cannot create.
    msg = gitops.clone_problem(tmp_path / "not-a-repo")
    assert msg is not None and msg.startswith("the repository cannot be cloned")
