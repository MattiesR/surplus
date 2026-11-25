"""Run service: drives one agent run through the state machine (plan §7.5, §8). The I/O shell of the runner.

Pipeline: preflight → prepare (bundle snapshot, clone, branch, prompt, container) → run the CLI and stream its
output → wrap up (commit leftovers inside the container, fetch the branch into the project) → verify (the
project's test command inside the same container) → report (validated JSON, one repair turn, fallback) →
awaiting feedback (the user merges or discards; agents never do).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import Clock
from surplus.collector.jsonl import UsageTag
from surplus.collector.store import ingest_logs, insert_readings, latest_readings
from surplus.config import Paths, Settings
from surplus.db.models import RunRow
from surplus.domain.events import Event, EventBus
from surplus.domain.models import FIVE_HOUR
from surplus.projects import repo
from surplus.projects.backlog import Backlog, BacklogItem, Size
from surplus.projects.registry import ProjectRegistry
from surplus.projects.service import InvalidDocumentError, ProjectService
from surplus.runner import gitops, prompt, report
from surplus.runner.container import TOKEN_ENV, ContainerRuntime, RunContainerSpec, instance_id
from surplus.runner.guard import Limits, budget_pct, over_limit
from surplus.runner.state import ACTIVE, ExitReason, RunState, recover, transition
from surplus.runner.stream import LogEntry, StreamState, handle_line, spent_pct

log = structlog.get_logger(__name__)

S = RunState
INGEST_EVERY_SECONDS = 30
# Inside the container, git must not run the agent's hooks or fsmonitor (they would hang or mislead).
IN_CONTAINER_GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


class PreflightError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class RunNotFoundError(KeyError):
    pass


class RunActionError(ValueError):
    pass


@dataclass(frozen=True)
class Preflight:
    problems: list[str]
    warnings: list[str]
    budget_pct: float | None
    model: str
    max_turns: int

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class _Live:
    """In-memory state of a run that is being driven."""

    stop_reason: ExitReason | None = None
    stop_detail: str | None = None
    seq: int = 0
    stream: StreamState = field(default_factory=StreamState)


def run_name(run_id: int) -> str:
    return f"r{run_id}"


class RunService:
    def __init__(
        self,
        sm: sessionmaker[Session],
        paths: Paths,
        settings: Settings,
        clock: Clock,
        bus: EventBus,
        registry: ProjectRegistry,
        projects: ProjectService,
        runtime: ContainerRuntime,
        token: Callable[[], str | None],
        claude_bin: Callable[[], Path | None],
    ) -> None:
        self.sm, self.paths, self.settings, self.clock, self.bus = sm, paths, settings, clock, bus
        self.registry, self.projects, self.runtime = registry, projects, runtime
        self._token = token
        self._claude_bin = claude_bin
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._live: dict[int, _Live] = {}
        self._start_lock = asyncio.Lock()
        self.limits = Limits(settings.run_max_five_hour_pct, settings.run_max_weekly_pct)
        self.instance = instance_id(paths.data_dir)

    def container_name(self, run_id: int) -> str:
        return f"surplus-run-{self.instance}-{run_id}"

    # ------------------------------------------------------------------ queries

    def run_dir(self, run_id: int) -> Path:
        return self.paths.runs_dir / run_name(run_id)

    def get(self, run_id: int) -> RunRow:
        with self.sm() as session:
            row = session.get(RunRow, run_id)
            if row is None:
                raise RunNotFoundError(run_id)
            session.expunge(row)
            return row

    def list_runs(self, project_id: str | None = None, limit: int = 50) -> list[RunRow]:
        with self.sm() as session:
            stmt = select(RunRow).order_by(RunRow.id.desc()).limit(limit)
            if project_id:
                stmt = stmt.where(RunRow.project_id == project_id)
            rows = list(session.scalars(stmt))
            for r in rows:
                session.expunge(r)
            return rows

    def latest_per_project(self) -> dict[str, RunRow]:
        """The most recent run of every project (for the project cards)."""
        with self.sm() as session:
            newest = (
                select(RunRow.project_id, func.max(RunRow.id).label("id"))
                .group_by(RunRow.project_id)
                .subquery()
            )
            rows = list(session.scalars(select(RunRow).join(newest, RunRow.id == newest.c.id)))
            for r in rows:
                session.expunge(r)
        return {r.project_id: r for r in rows}

    def active_count(self) -> int:
        with self.sm() as session:
            return len(
                list(session.scalars(select(RunRow.id).where(RunRow.state.in_([s.value for s in ACTIVE]))))
            )

    def log_entries(self, run_id: int, after: int = 0) -> list[dict[str, Any]]:
        path = self.run_dir(run_id) / "log.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                entry = json.loads(line)
                if entry.get("seq", 0) > after:
                    out.append(entry)
        return out

    def test_output(self, run_id: int) -> str:
        path = self.run_dir(run_id) / "tests.txt"
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    def diff(self, run_id: int) -> tuple[str, bool]:
        row = self.get(run_id)
        if not (row.branch and row.base_sha and row.head_sha):
            return "", False
        folder = self.projects.folder(row.project_id)
        if not gitops.branch_exists(folder, row.branch):
            return "", False
        return gitops.diff(folder, row.base_sha, row.branch)

    # ------------------------------------------------------------------ preflight

    def preflight(self, project_id: str, task_id: str, model: str | None = None) -> Preflight:
        problems: list[str] = []
        warnings: list[str] = []
        snap = self.projects.snapshot(project_id)
        meta = snap.meta
        chosen_model = model or (meta.model if meta else "sonnet")
        if meta is None:
            problems.append("project.md has errors; fix them first")
        elif meta.status != repo.ProjectStatus.ACTIVE:
            problems.append(f"the project is {meta.status.value}")
        if snap.backlog is None:
            problems.append("backlog.md has errors; fix them first")
        else:
            try:
                task = snap.backlog.get(task_id)
            except KeyError:
                problems.append(f"no task {task_id} in backlog.md")
            else:
                if task.done:
                    problems.append(f"{task_id} is already done")
                if task.proposed:
                    problems.append(f"{task_id} is a proposal; accept it first")
                if "blocked" in task.flags:
                    problems.append(f"{task_id} is blocked")
                if task.size is Size.L:
                    warnings.append(
                        f"{task_id} is large; the agent will do a first slice and suggest the rest"
                    )
        state = gitops.repo_state(snap.root)
        if state.head_sha is None:
            problems.append("the repository has no commits yet; make a first commit")
        if state.dirty_files:
            warnings.append(
                f"uncommitted changes ({', '.join(state.dirty_files[:3])}) are not included: "
                "the agent works from HEAD"
            )
        if self.active_count() >= self.settings.max_concurrent_runs:
            problems.append("another run is in progress")
        readings = self.latest_readings()
        if not self.resets_fresh():
            problems.append(
                "no fresh reset times from Claude (older than 1 h); runs pause until the probe succeeds"
            )
        reason = over_limit(readings, self.limits)
        if reason:
            problems.append(f"limit: {reason}")
        if not any(r.bucket == FIVE_HOUR for r in readings):
            problems.append("no 5-hour reading yet; run a probe first")
        if self._token() is None:
            problems.append(
                "no CLAUDE_CODE_OAUTH_TOKEN in ~/.config/surplus/secrets.env (run `claude setup-token`)"
            )
        if self._claude_bin() is None:
            problems.append("the Claude CLI binary was not found on the host")
        if chosen_model == "opus":
            warnings.append("opus uses the limits much faster than sonnet")
        return Preflight(
            problems,
            warnings,
            budget_pct(readings, self.limits),
            chosen_model,
            self.settings.default_max_turns,
        )

    def latest_readings(self) -> list[Any]:
        with self.sm() as session:
            return latest_readings(session)

    def resets_fresh(self) -> bool:
        from surplus.collector import reset_watcher
        from surplus.collector.store import latest_reset_reading

        with self.sm() as session:
            return reset_watcher.reset_times_fresh(latest_reset_reading(session), self.clock.now())

    # ------------------------------------------------------------------ commands

    async def start(
        self, project_id: str, task_id: str, model: str | None = None, max_turns: int | None = None
    ) -> RunRow:
        async with self._start_lock:
            if not await self.runtime.image_exists(self.settings.runner_image):
                raise PreflightError(
                    [f"runner image {self.settings.runner_image} is missing: run `surplus runner build`"]
                )
            pf = await asyncio.to_thread(self.preflight, project_id, task_id, model)
            if not pf.ok:
                raise PreflightError(pf.problems)
            snap = self.projects.snapshot(project_id)
            assert snap.backlog is not None
            task = snap.backlog.get(task_id)
            now = self.clock.now()
            with self.sm.begin() as session:
                row = RunRow(
                    project_id=project_id,
                    task_id=task.id,
                    task_title=task.title,
                    state=S.QUEUED.value,
                    model=pf.model,
                    max_turns=max_turns or pf.max_turns,
                    budget_pct=pf.budget_pct,
                    commits=0,
                    base_dirty=any("uncommitted" in w for w in pf.warnings),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                run_id = row.id
            log.info(
                "run.queued",
                run=run_id,
                project=project_id,
                task=task_id,
                model=pf.model,
                budget_pct=pf.budget_pct,
            )
            self._live[run_id] = _Live()
            self._publish_changed(run_id)
            self._tasks[run_id] = asyncio.create_task(self._drive(run_id), name=f"run-{run_id}")
            return self.get(run_id)

    async def stop(
        self, run_id: int, reason: ExitReason = ExitReason.USER_STOP, detail: str | None = None
    ) -> None:
        """Ask the agent to wrap up: its work is still committed, verified and reported."""
        live = self._live.get(run_id)
        row = self.get(run_id)
        if live is None or row.state not in (S.QUEUED.value, S.PREPARING.value, S.RUNNING.value):
            raise RunActionError(f"run {run_name(run_id)} is not running")
        if live.stop_reason is None:
            live.stop_reason, live.stop_detail = reason, detail
            self._log(run_id, "surplus", f"stop requested: {detail or reason.value}")
            if row.container and row.state == S.RUNNING.value:
                await self.runtime.signal_agent(row.container)

    async def cancel(self, run_id: int) -> None:
        """Stop now without a report. The clone stays on disk; the branch is not brought back."""
        task = self._tasks.get(run_id)
        if task is None or task.done():
            raise RunActionError(f"run {run_name(run_id)} is not active")
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def merge(self, run_id: int) -> RunRow:
        row = self.get(run_id)
        if row.state != S.AWAITING_FEEDBACK.value or not row.branch:
            raise RunActionError("only a finished run with a branch can be merged")
        if not row.head_sha:
            raise RunActionError("the run made no commits; discard it instead")
        folder = self.projects.folder(row.project_id)
        title = f"{row.task_id} {row.task_title or ''}".strip()
        msg = f"Merge {row.branch} (Surplus run {run_name(run_id)}: {title})"
        try:
            sha = await asyncio.to_thread(gitops.merge, folder, row.branch, row.base_branch, msg)
        except gitops.GitError as exc:
            raise RunActionError(str(exc)) from exc
        log.info("run.merged", run=run_id, branch=row.branch, sha=sha)
        self._finish_feedback(run_id, "merged")
        return self.get(run_id)

    async def discard(self, run_id: int) -> RunRow:
        row = self.get(run_id)
        if row.state != S.AWAITING_FEEDBACK.value:
            raise RunActionError("only a run waiting for feedback can be discarded")
        if row.branch:
            await asyncio.to_thread(gitops.delete_branch, self.projects.folder(row.project_id), row.branch)
        log.info("run.discarded", run=run_id, branch=row.branch)
        self._finish_feedback(run_id, "discarded")
        return self.get(run_id)

    def _finish_feedback(self, run_id: int, resolution: str) -> None:
        self._update(run_id, state=S.DONE, resolution=resolution)
        clone = self.run_dir(run_id) / "repo"
        shutil.rmtree(clone, ignore_errors=True)

    async def recover(self) -> None:
        """At startup: runs that were active when Surplus stopped are marked interrupted; work is salvaged."""
        with self.sm() as session:
            stale = list(session.scalars(select(RunRow).where(RunRow.state.in_([s.value for s in ACTIVE]))))
            for r in stale:
                session.expunge(r)
        for row in stale:
            new_state = recover(RunState(row.state))
            if new_state is None:
                continue
            head = None
            clone = self.run_dir(row.id) / "repo"
            if row.branch and clone.is_dir():
                with contextlib.suppress(gitops.GitError, OSError, repo.UnsafePathError, KeyError):
                    head = await asyncio.to_thread(
                        gitops.fetch_branch, self.projects.folder(row.project_id), clone, row.branch
                    )
            if row.container:
                await self.runtime.remove(row.container)
            self._update(
                row.id,
                state=new_state,
                exit_reason=ExitReason.INTERRUPTED,
                error="Surplus stopped during this run"
                + (f"; branch {row.branch} was saved" if head else ""),
                head_sha=head,
                ended_at=self.clock.now(),
            )
            log.warning("run.recovered", run=row.id, previous=row.state, branch_saved=bool(head))
        # Containers left behind by runs that are not active any more.
        active = {s.value for s in ACTIVE}
        known = {self.container_name(r.id) for r in self.list_runs(limit=200) if r.state in active}
        for name in await self.runtime.list_run_containers(self.instance):
            if name not in known:
                await self.runtime.remove(name)

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    # ------------------------------------------------------------------ internals: persistence + events

    def _update(self, run_id: int, state: RunState | None = None, **fields: Any) -> None:
        with self.sm.begin() as session:
            row = session.get(RunRow, run_id)
            assert row is not None
            if state is not None and state.value != row.state:
                transition(RunState(row.state), state)
                log.info("run.transition", run=run_id, frm=row.state, to=state.value, **_loggable(fields))
                row.state = state.value
            for key, value in fields.items():
                setattr(row, key, value.value if isinstance(value, ExitReason) else value)
            row.updated_at = self.clock.now()
        if state is not None:
            self._log(run_id, "surplus", f"state → {state.value}")
        self._publish_changed(run_id)

    def _publish_changed(self, run_id: int) -> None:
        row = self.get(run_id)
        self.bus.publish(
            Event("run.changed", {"id": run_id, "project_id": row.project_id, "state": row.state})
        )

    def _log(self, run_id: int, kind: str, text: str, is_error: bool = False) -> None:
        self._write_entry(run_id, LogEntry(self.clock.now(), kind, text, is_error))  # type: ignore[arg-type]

    def _write_entry(self, run_id: int, entry: LogEntry) -> None:
        live = self._live.setdefault(run_id, _Live())
        live.seq += 1
        data = {"seq": live.seq, **entry.to_json()}
        path = self.run_dir(run_id) / "log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.bus.publish(Event("run.log", {"id": run_id, "entry": data}))

    # ------------------------------------------------------------------ internals: the pipeline

    async def _drive(self, run_id: int) -> None:
        container: str | None = None
        try:
            container = await self._execute(run_id)
        except asyncio.CancelledError:
            row = self.get(run_id)
            if row.state in {s.value for s in ACTIVE}:
                self._update(
                    run_id, state=S.CANCELLED, exit_reason=ExitReason.USER_STOP, ended_at=self.clock.now()
                )
            container = row.container
            raise
        except PreflightError as exc:
            live = self._live.get(run_id)
            stopped = live is not None and live.stop_reason is ExitReason.USER_STOP
            self._update(
                run_id,
                state=S.PAUSED_BY_USER
                if stopped
                else S.BUDGET_EXHAUSTED
                if any(p.startswith("limit:") for p in exc.problems)
                else S.FAILED,
                exit_reason=ExitReason.PREFLIGHT,
                error=str(exc),
                ended_at=self.clock.now(),
            )
        except Exception as exc:
            log.exception("run.failed", run=run_id)
            self._log(run_id, "error", f"internal error: {exc}", is_error=True)
            row = self.get(run_id)
            container = row.container
            if row.state in {s.value for s in ACTIVE}:
                self._update(
                    run_id,
                    state=S.FAILED,
                    exit_reason=ExitReason.INTERNAL_ERROR,
                    error=str(exc)[:2000],
                    ended_at=self.clock.now(),
                )
        finally:
            if container:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self.runtime.remove(container))
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._ingest_run_logs, run_id)
            self._tasks.pop(run_id, None)
            self._live.pop(run_id, None)

    async def _execute(self, run_id: int) -> str | None:
        row = self.get(run_id)
        live = self._live[run_id]
        folder = self.projects.folder(row.project_id)
        rdir = self.run_dir(row.id)
        clone = rdir / "repo"
        cfg = rdir / "claude"

        # ---- preparing
        self._update(run_id, state=S.PREPARING)
        snap = self.projects.snapshot(row.project_id)
        if snap.backlog is None or snap.meta is None:
            raise PreflightError(["project files have errors"])
        task = snap.backlog.get(row.task_id or "")
        token = self._token()
        claude_bin = self._claude_bin()
        if token is None or claude_bin is None:
            raise PreflightError(["token or CLI missing"])
        state = await asyncio.to_thread(gitops.repo_state, folder)
        branch = await asyncio.to_thread(gitops.unique_branch, folder, prompt.branch_name(task))
        await asyncio.to_thread(gitops.snapshot_bundle, folder, rdir / "snapshot.bundle")
        base_sha = await asyncio.to_thread(gitops.prepare_clone, folder, clone, branch)
        cfg.mkdir(parents=True, exist_ok=True)
        recent = _recent_runs(repo.ProjectFiles(folder).runs_dir)
        composed = prompt.compose(
            name=snap.meta.name,
            description=snap.description_md,
            goals=snap.goals.goals,
            task=task,
            decisions=snap.decisions,
            recent_runs=recent,
            branch=branch,
            test_command=snap.meta.test_command,
            max_turns=row.max_turns,
        )
        (rdir / "prompt.md").write_text(
            composed.prompt + "\n\n---\n\n" + composed.system_append, encoding="utf-8"
        )
        container = self.container_name(run_id)
        self._update(run_id, branch=branch, base_branch=state.branch, base_sha=base_sha, container=container)
        self._log(
            run_id, "surplus", f"branch {branch} from {state.branch or 'detached HEAD'} @ {base_sha[:10]}"
        )
        await self.runtime.ensure_network()
        await self.runtime.ensure_proxy(self.settings.runner_image, self.settings.egress_allow)
        lifetime = (self.settings.run_timeout_minutes + self.settings.test_timeout_minutes + 15) * 60
        await self.runtime.start(
            RunContainerSpec(
                name=container,
                run_id=run_name(run_id),
                instance=self.instance,
                image=self.settings.runner_image,
                workspace=clone,
                claude_config=cfg,
                claude_bin=claude_bin,
                lifetime_seconds=lifetime,
                memory=self.settings.run_memory,
                cpus=self.settings.run_cpus,
            )
        )
        if live.stop_reason is not None:
            raise PreflightError([f"stopped before start: {live.stop_detail or live.stop_reason.value}"])

        # ---- running
        before = {r.bucket: r.used_pct for r in self.latest_readings()}
        self._update(run_id, state=S.RUNNING, started_at=self.clock.now())
        argv = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            row.model,
            "--max-turns",
            str(row.max_turns),
            "--permission-mode",
            "bypassPermissions",  # only ever inside the container (plan §9.2)
            "--strict-mcp-config",
            "--append-system-prompt",
            composed.system_append,
        ]
        await self._run_agent(run_id, container, argv, composed.prompt.encode("utf-8"), token)
        st = live.stream
        exit_reason = live.stop_reason or (
            ExitReason.MAX_TURNS
            if st.result_subtype == "error_max_turns"
            else ExitReason.AGENT_ERROR
            if (st.is_error or not st.finished)
            else ExitReason.COMPLETED
        )
        spent = spent_pct(st, FIVE_HOUR, before.get(FIVE_HOUR))

        # ---- wrapping up
        self._update(
            run_id,
            state=S.WRAPPING_UP,
            exit_reason=exit_reason,
            session_id=st.session_id,
            num_turns=st.num_turns,
            spent_pct_est=spent,
        )
        status = await self.runtime.exec(container, [*IN_CONTAINER_GIT, "status", "--porcelain"], timeout=120)
        if status.returncode == 0 and status.output.strip():
            await self.runtime.exec(container, [*IN_CONTAINER_GIT, "add", "--all"], timeout=300)
            await self.runtime.exec(
                container,
                [
                    *IN_CONTAINER_GIT,
                    "commit",
                    "--no-verify",
                    "--quiet",
                    "-m",
                    "chore(surplus): uncommitted work left by the agent",
                ],
                timeout=300,
            )
            self._log(run_id, "surplus", "committed changes the agent left uncommitted")
        try:
            head = await asyncio.to_thread(gitops.fetch_branch, folder, clone, branch)
        except gitops.GitError as exc:
            # e.g. the agent deleted its clone's .git: nothing to bring back; the project is untouched.
            head = None
            self._log(run_id, "error", f"could not fetch {branch} from the run clone: {exc}", is_error=True)
        commits = await asyncio.to_thread(gitops.commits_between, folder, base_sha, branch) if head else []
        files = await asyncio.to_thread(gitops.files_between, folder, base_sha, branch) if head else []
        if head == base_sha:
            commits, files = [], []
            await asyncio.to_thread(gitops.delete_branch, folder, branch)  # nothing to review
        self._update(run_id, head_sha=head if commits else None, commits=len(commits))
        self._log(run_id, "surplus", f"{len(commits)} commit(s) on {branch}; {len(files)} file(s) changed")

        # ---- verifying
        self._update(run_id, state=S.VERIFYING)
        verified = "none"
        test_out = ""
        if snap.meta.test_command:
            self._log(run_id, "surplus", f"running tests: {snap.meta.test_command}")
            res = await self.runtime.exec(
                container,
                ["sh", "-lc", snap.meta.test_command],
                timeout=self.settings.test_timeout_minutes * 60,
            )
            verified = "pass" if res.returncode == 0 and not res.timed_out else "fail"
            test_out = res.output + ("\n[timed out]" if res.timed_out else "")
            (rdir / "tests.txt").write_text(test_out, encoding="utf-8")
            self._log(
                run_id, "surplus", f"tests {verified} (exit {res.returncode})", is_error=verified == "fail"
            )
        self._update(run_id, verified_tests=verified)

        # ---- reporting
        self._update(run_id, state=S.REPORTING)
        extraction = report.extract_report(st.final_text)
        rep, source = extraction.report, "agent"
        if rep is None and st.session_id and exit_reason is not ExitReason.BUDGET:
            self._log(run_id, "surplus", f"report invalid ({extraction.error}); asking once for a repair")
            rep = await self._repair(run_id, container, st.session_id, extraction.error or "", token)
            source = "repaired"
        if rep is None:
            rep, source = report.fallback_report(commits, files, verified), "fallback"
        ended = self.clock.now()
        facts = report.RunFacts(
            run_id=run_name(run_id),
            project_id=row.project_id,
            task_id=task.id,
            task_title=task.title,
            model=row.model,
            branch=branch,
            started_at=self.get(run_id).started_at,
            ended_at=ended,
            state=S.AWAITING_FEEDBACK.value,
            exit_reason=exit_reason.value,
            verified_tests=verified,
            spent_pct_est=spent,
            commits=len(commits),
            report_source=source,  # type: ignore[arg-type]
        )
        report_path = await asyncio.to_thread(self._write_report, folder, row, facts, rep, test_out)
        await asyncio.to_thread(self._apply_to_backlog, row.project_id, task, rep, verified, bool(commits))
        self._update(
            run_id,
            state=S.AWAITING_FEEDBACK,
            report_json=rep.model_dump_json(),
            report_source=source,
            report_path=str(report_path),
            ended_at=ended,
        )
        self.bus.publish(Event("run.reported", {"id": run_id, "project_id": row.project_id}))
        return container

    async def _run_agent(
        self, run_id: int, container: str, argv: list[str], stdin: bytes, token: str
    ) -> None:
        live = self._live[run_id]
        stream = await self.runtime.exec_stream(container, argv, stdin, {TOKEN_ENV: token})
        timeout = self.settings.run_timeout_minutes * 60

        async def watchdog() -> None:
            await asyncio.sleep(timeout)
            with contextlib.suppress(RunActionError):
                await self.stop(
                    run_id, ExitReason.TIMEOUT, f"wall-clock limit of {self.settings.run_timeout_minutes} min"
                )
            await asyncio.sleep(self.settings.run_stop_grace_seconds)
            await self.runtime.remove(container)  # last resort: the CLI ignored the stop

        async def ingester() -> None:
            while True:
                await asyncio.sleep(INGEST_EVERY_SECONDS)
                await asyncio.to_thread(self._ingest_run_logs, run_id)

        guards = [asyncio.create_task(watchdog()), asyncio.create_task(ingester())]
        try:
            async for line in stream.lines():
                res = handle_line(live.stream, line, self.clock.now())
                for entry in res.entries:
                    self._write_entry(run_id, entry)
                if res.readings:
                    with self.sm.begin() as session:
                        insert_readings(session, res.readings)
                    self.bus.publish(Event("usage.updated", {"readings": len(res.readings)}))
                    reason = over_limit(res.readings, self.limits)
                    if reason and live.stop_reason is None:
                        await self.stop(run_id, ExitReason.BUDGET, reason)
            rc = await stream.wait()
            if rc != 0 and not live.stream.finished:
                self._log(
                    run_id, "error", f"CLI exited with {rc}: {stream.stderr_tail()[-1000:]}", is_error=True
                )
        finally:
            for g in guards:
                g.cancel()
            await asyncio.gather(*guards, return_exceptions=True)

    async def _repair(
        self, run_id: int, container: str, session_id: str, error: str, token: str
    ) -> report.RunReport | None:
        argv = [
            "claude",
            "-p",
            "--resume",
            session_id,
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            "haiku",
            "--max-turns",
            "1",
            "--tools",
            "",
        ]
        state = StreamState()
        stream = await self.runtime.exec_stream(
            container, argv, report.repair_prompt(error).encode(), {TOKEN_ENV: token}
        )
        async for line in stream.lines():
            res = handle_line(state, line, self.clock.now())
            if res.readings:
                with self.sm.begin() as session:
                    insert_readings(session, res.readings)
        await stream.wait()
        extraction = report.extract_report(state.final_text)
        if extraction.report is None:
            self._log(
                run_id,
                "surplus",
                f"repair failed ({extraction.error}); using a fallback report",
                is_error=True,
            )
        return extraction.report

    def _write_report(
        self, folder: Path, row: RunRow, facts: report.RunFacts, rep: report.RunReport, test_out: str
    ) -> Path:
        stamp = (facts.started_at or self.clock.now()).strftime("%Y-%m-%dT%H%MZ")
        path = repo.ProjectFiles(folder).runs_dir / f"{stamp}-{run_name(row.id)}.md"
        repo.atomic_write(path, report.render_run_md(facts, rep, test_out))
        return path

    def _apply_to_backlog(
        self, project_id: str, task: BacklogItem, rep: report.RunReport, verified: str, has_commits: bool
    ) -> None:
        """Tick the task off only if Surplus's own test run passed; add suggestions as `proposed` items."""

        def fn(doc: Backlog) -> None:
            if verified == "pass" and has_commits:
                with contextlib.suppress(KeyError):
                    doc.get(task.id).done = True
            known = {" ".join(i.title.lower().split()) for i in doc.items}
            for sug in rep.next_suggestions:
                if " ".join(sug.title.lower().split()) in known:
                    continue
                item = BacklogItem(
                    id=doc.next_id(), title=sug.title, priority=2, size=Size(sug.size), flags=["proposed"]
                )
                item.validate()
                doc.add(item)

        try:
            self.projects.edit_backlog_latest(project_id, fn)
        except (repo.ConflictError, InvalidDocumentError, ValueError) as exc:
            log.warning("run.backlog_update_failed", project=project_id, task=task.id, error=str(exc))

    def _ingest_run_logs(self, run_id: int) -> None:
        row = self.get(run_id)
        root = self.run_dir(run_id) / "claude" / "projects"
        if root.is_dir():
            ingest_logs(
                self.sm,
                (root,),
                self.clock,
                UsageTag(run_id=run_name(run_id), project_id=row.project_id),
                source_name="agent_log",
            )


def _recent_runs(runs_dir: Path) -> list[tuple[str, str]]:
    if not runs_dir.is_dir():
        return []
    files = sorted(runs_dir.glob("*.md"), reverse=True)[: prompt.MAX_RECENT_RUNS]
    return [(f.name, f.read_text(encoding="utf-8", errors="replace")) for f in files]


def _loggable(fields: dict[str, Any]) -> dict[str, Any]:
    keep = ("exit_reason", "branch", "head_sha", "commits", "verified_tests", "spent_pct_est", "error")
    out: dict[str, Any] = {}
    for k in keep:
        if k in fields:
            v = fields[k]
            out[k] = v.value if isinstance(v, ExitReason) else v
    return out
