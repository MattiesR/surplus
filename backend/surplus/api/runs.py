"""Runs API (plan §8, §11 "Run detail"). Runs are manual only in Phase 3: the user presses "Run now"."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from surplus.db.models import RunRow
from surplus.projects.service import NotFoundError
from surplus.runner.report import RunReport
from surplus.runner.service import (
    PreflightError,
    RunActionError,
    RunNotFoundError,
    RunService,
    can_continue,
    run_name,
)

router = APIRouter(prefix="/api", tags=["runs"])


class ErrorOut(BaseModel):
    detail: str
    problems: list[str] = Field(default_factory=list)


class RunOut(BaseModel):
    id: int
    name: str
    project_id: str
    task_id: str | None
    task_title: str | None
    state: str
    exit_reason: str | None
    model: str
    max_turns: int
    budget_pct: float | None
    spent_pct_est: float | None
    branch: str | None
    base_branch: str | None
    commits: int
    num_turns: int | None
    verified_tests: str | None
    report_source: str | None
    resolution: str | None
    error: str | None
    base_dirty: bool
    effort: str | None
    cap_five_hour_pct: float | None = Field(description="This run's 5-hour hard stop, in %")
    cap_weekly_pct: float | None
    timeout_minutes: int | None
    parent_run_id: int | None = Field(description="Set on a continuation: the run it carries on")
    trigger: str = Field(description="manual: you pressed Run; auto: the dispatcher started it")
    mode: str = Field(description="work, or split: propose smaller tasks instead of implementing")
    can_continue: bool = Field(description="Stopped by its cap, turns or time; the user may let it continue")
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None


class RunDetailOut(RunOut):
    continued_by: int | None = Field(description="The continuation started from this run, if any")
    report: RunReport | None
    report_path: str | None
    test_output: str


class LogEntryOut(BaseModel):
    seq: int
    ts: datetime
    kind: str
    text: str
    is_error: bool


class DiffOut(BaseModel):
    text: str
    truncated: bool


class RunIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(pattern=r"^T-\d{3,}$")
    model: Literal["haiku", "sonnet", "opus"] | None = None
    max_turns: int | None = Field(default=None, ge=1, le=200)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class ContinueIn(BaseModel):
    max_five_hour_pct: float = Field(gt=0, le=100, description="Hard stop for the continuation, in %")


class PreflightOut(BaseModel):
    ok: bool
    problems: list[str]
    warnings: list[str]
    budget_pct: float | None = Field(description="Room in the 5-hour window before the hard stop, in pp")
    model: str
    max_turns: int
    effort: str | None
    cap_five_hour_pct: float
    cap_weekly_pct: float
    timeout_minutes: int
    image: bool


def _svc(request: Request) -> RunService:
    svc: RunService = request.app.state.runs
    return svc


def _out(row: RunRow) -> RunOut:
    return RunOut(
        id=row.id,
        name=run_name(row.id),
        project_id=row.project_id,
        task_id=row.task_id,
        task_title=row.task_title,
        state=row.state,
        exit_reason=row.exit_reason,
        model=row.model,
        max_turns=row.max_turns,
        budget_pct=row.budget_pct,
        spent_pct_est=row.spent_pct_est,
        branch=row.branch,
        base_branch=row.base_branch,
        commits=row.commits,
        num_turns=row.num_turns,
        verified_tests=row.verified_tests,
        report_source=row.report_source,
        resolution=row.resolution,
        error=row.error,
        base_dirty=row.base_dirty,
        effort=row.effort,
        cap_five_hour_pct=row.cap_five_hour_pct,
        cap_weekly_pct=row.cap_weekly_pct,
        timeout_minutes=row.timeout_minutes,
        parent_run_id=row.parent_run_id,
        trigger=row.trigger,
        mode=row.mode,
        can_continue=can_continue(row),
        created_at=row.created_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
    )


def _get(svc: RunService, run_id: int) -> RunRow:
    try:
        return svc.get(run_id)
    except RunNotFoundError:
        raise HTTPException(404, f"no run {run_id}") from None


ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
    422: {"model": ErrorOut},
}


@router.get("/runs")
def list_runs(request: Request, project_id: str | None = Query(default=None)) -> list[RunOut]:
    return [_out(r) for r in _svc(request).list_runs(project_id)]


@router.get("/runs/{run_id}", responses=ERRORS)
def get_run(request: Request, run_id: int) -> RunDetailOut:
    svc = _svc(request)
    row = _get(svc, run_id)
    rep = RunReport.model_validate(json.loads(row.report_json)) if row.report_json else None
    return RunDetailOut(
        **_out(row).model_dump(),
        continued_by=svc.continuation_of(run_id),
        report=rep,
        report_path=row.report_path,
        test_output=svc.test_output(run_id)[-20_000:],
    )


@router.get("/runs/{run_id}/log", responses=ERRORS)
def run_log(request: Request, run_id: int, after: int = Query(default=0, ge=0)) -> list[LogEntryOut]:
    svc = _svc(request)
    _get(svc, run_id)
    return [LogEntryOut.model_validate(e) for e in svc.log_entries(run_id, after)]


@router.get("/runs/{run_id}/diff", responses=ERRORS)
def run_diff(request: Request, run_id: int) -> DiffOut:
    svc = _svc(request)
    _get(svc, run_id)
    try:
        text, truncated = svc.diff(run_id)
    except NotFoundError:
        raise HTTPException(404, "the project is gone") from None
    return DiffOut(text=text, truncated=truncated)


@router.post("/runs/preflight", responses=ERRORS)
async def preflight(request: Request, body: RunIn) -> PreflightOut:
    svc = _svc(request)
    try:
        pf = svc.preflight(body.project_id, body.task_id, body.model, body.effort)
    except NotFoundError:
        raise HTTPException(404, f"no project {body.project_id}") from None
    image = await svc.runtime.image_exists(svc.settings.runner_image)
    problems = list(pf.problems)
    if not image:
        problems.insert(0, f"runner image {svc.settings.runner_image} is missing: run `surplus runner build`")
    return PreflightOut(
        ok=not problems,
        problems=problems,
        warnings=pf.warnings,
        budget_pct=pf.budget_pct,
        model=pf.model,
        max_turns=body.max_turns or pf.max_turns,
        effort=pf.effort,
        cap_five_hour_pct=pf.cap_five_hour_pct,
        cap_weekly_pct=pf.cap_weekly_pct,
        timeout_minutes=pf.timeout_minutes,
        image=image,
    )


@router.post("/runs", status_code=201, responses=ERRORS)
async def start_run(request: Request, body: RunIn) -> RunOut:
    try:
        row = await _svc(request).start(
            body.project_id, body.task_id, body.model, body.max_turns, body.effort
        )
    except NotFoundError:
        raise HTTPException(404, f"no project {body.project_id}") from None
    except PreflightError as exc:
        raise HTTPException(409, {"detail": "run not started", "problems": exc.problems}) from None
    return _out(row)


@router.post("/runs/{run_id}/stop", responses=ERRORS)
async def stop_run(request: Request, run_id: int) -> RunOut:
    svc = _svc(request)
    _get(svc, run_id)
    try:
        await svc.stop(run_id)
    except RunActionError as exc:
        raise HTTPException(409, str(exc)) from None
    return _out(svc.get(run_id))


@router.post("/runs/{run_id}/cancel", responses=ERRORS)
async def cancel_run(request: Request, run_id: int) -> RunOut:
    svc = _svc(request)
    _get(svc, run_id)
    try:
        await svc.cancel(run_id)
    except RunActionError as exc:
        raise HTTPException(409, str(exc)) from None
    return _out(svc.get(run_id))


@router.post("/runs/{run_id}/merge", responses=ERRORS)
async def merge_run(request: Request, run_id: int) -> RunOut:
    svc = _svc(request)
    _get(svc, run_id)
    try:
        return _out(await svc.merge(run_id))
    except RunActionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/runs/{run_id}/discard", responses=ERRORS)
async def discard_run(request: Request, run_id: int) -> RunOut:
    svc = _svc(request)
    _get(svc, run_id)
    try:
        return _out(await svc.discard(run_id))
    except RunActionError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/runs/{run_id}/continue", status_code=201, responses=ERRORS)
async def continue_run(request: Request, run_id: int, body: ContinueIn) -> RunOut:
    """The user's permission to go on after a run stopped at its cap, turn or time limit (ADR 0008)."""
    svc = _svc(request)
    _get(svc, run_id)
    try:
        return _out(await svc.continue_run(run_id, body.max_five_hour_pct))
    except RunActionError as exc:
        raise HTTPException(409, str(exc)) from None
    except PreflightError as exc:
        raise HTTPException(409, {"detail": "run not continued", "problems": exc.problems}) from None
