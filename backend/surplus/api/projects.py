"""Projects API: list, create (wizard), detail, whole-file edits, settings and backlog operations."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from surplus.projects import assist, repo
from surplus.projects import backlog as bl
from surplus.projects.registry import ProjectRegistry
from surplus.projects.service import InvalidDocumentError, NotFoundError, ProjectService
from surplus.secrets import CLAUDE_TOKEN, has_secret

router = APIRouter(prefix="/api")

Doc = Literal["project", "goals", "backlog", "decisions"]


# ---------------------------------------------------------------- schemas


class ErrorOut(BaseModel):
    detail: str
    line: int | None = None
    current_hash: str | None = None


class DocError(BaseModel):
    doc: str
    message: str
    line: int | None = None


class ProjectCard(BaseModel):
    id: str
    name: str
    path: str
    status: str
    priority: int
    missing: bool
    errors: list[DocError]
    tasks_open: int
    tasks_done: int
    tasks_proposed: int
    open_questions: int = 0  # Phase 5
    last_run_at: datetime | None = None
    last_run_id: int | None = None
    last_run_state: str | None = None
    last_run_resolution: str | None = None


class GoalOut(BaseModel):
    id: str
    title: str
    acceptance: list[str]


class TaskOut(BaseModel):
    id: str
    title: str
    priority: int
    size: str | None
    done: bool
    acceptance: str | None
    flags: list[str]
    notes: list[str]


class DecisionOut(BaseModel):
    heading: str
    question: str | None
    answer: str | None
    body: str


class DocOut(BaseModel):
    content: str
    hash: str


class ProjectDetail(BaseModel):
    id: str
    path: str
    name: str
    status: str
    priority: int
    model: str
    max_share_of_surplus: float
    test_command: str | None
    description_md: str
    goals: list[GoalOut]
    backlog: list[TaskOut] | None
    decisions: list[DecisionOut]
    runs: list[str]
    docs: dict[str, DocOut]
    errors: list[DocError]


class GoalIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    acceptance: list[str] = Field(default_factory=list, max_length=20)


class CreateProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=5000)
    goals: list[GoalIn] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    priority: int = Field(default=2, ge=1, le=5)
    model: Literal["haiku", "sonnet", "opus"] = "sonnet"
    test_command: str | None = Field(default=None, max_length=300)
    mode: Literal["new", "existing"] = "new"
    path: str | None = None
    git_init: bool = False


class WriteDocIn(BaseModel):
    content: str = Field(max_length=1_000_000)
    base_hash: str


class HashOut(BaseModel):
    hash: str


class SettingsIn(BaseModel):
    base_hash: str
    name: str | None = Field(default=None, min_length=1, max_length=120)
    status: Literal["active", "paused", "archived"] | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    model: Literal["haiku", "sonnet", "opus"] | None = None
    max_share_of_surplus: float | None = Field(default=None, gt=0, le=1)
    test_command: str | None = Field(default=None, max_length=300)
    clear_test_command: bool = False


class TaskIn(BaseModel):
    base_hash: str
    title: str = Field(min_length=1, max_length=300)
    priority: int = Field(default=2, ge=0, le=3)
    size: Literal["S", "M", "L"] | None = None
    acceptance: str | None = Field(default=None, max_length=1000)


class TaskPatch(BaseModel):
    base_hash: str
    title: str | None = Field(default=None, min_length=1, max_length=300)
    priority: int | None = Field(default=None, ge=0, le=3)
    size: Literal["S", "M", "L", ""] | None = None
    acceptance: str | None = Field(default=None, max_length=1000)
    done: bool | None = None
    proposed: bool | None = None
    blocked: bool | None = None


class ReorderIn(BaseModel):
    base_hash: str
    ids: list[str]


class AssistIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=3000)
    goals: list[str] = Field(default_factory=list, max_length=10)


class AssistOut(BaseModel):
    goals: list[GoalIn]


class SystemOut(BaseModel):
    projects_root: str
    podman: bool
    runner_image: bool
    token: bool = Field(default=False, description="A setup token is saved (its value is never exposed)")
    detail: str


# ---------------------------------------------------------------- helpers


def _svc(request: Request) -> ProjectService:
    svc: ProjectService = request.app.state.projects
    return svc


def _detail(pid: str, snap: repo.ProjectSnapshot) -> ProjectDetail:
    meta = snap.meta
    return ProjectDetail(
        id=pid,
        path=str(snap.root),
        name=meta.name if meta else str(snap.meta_values.get("name") or snap.root.name),
        status=meta.status.value if meta else "error",
        priority=meta.priority if meta else 5,
        model=meta.model if meta else "sonnet",
        max_share_of_surplus=meta.max_share_of_surplus if meta else 0.5,
        test_command=meta.test_command if meta else None,
        description_md=snap.description_md,
        goals=[GoalOut(id=g.id, title=g.title, acceptance=g.acceptance) for g in snap.goals.goals],
        backlog=None
        if snap.backlog is None
        else [
            TaskOut(
                id=i.id,
                title=i.title,
                priority=i.priority,
                size=i.size.value if i.size else None,
                done=i.done,
                acceptance=i.acceptance,
                flags=i.flags,
                notes=i.notes,
            )
            for i in snap.backlog.items
        ],
        decisions=[
            DecisionOut(heading=d.heading, question=d.question, answer=d.answer, body=d.body)
            for d in snap.decisions
        ],
        runs=snap.runs,
        docs={d.value: DocOut(content=snap.texts[d], hash=snap.hashes[d]) for d in repo.DocName},
        errors=[DocError(doc=e.doc.value, message=e.message, line=e.line) for e in snap.errors],
    )


def error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, repo.ConflictError):
        return JSONResponse(
            {
                "detail": "The file changed on disk since you opened it. Reload to see the new version.",
                "current_hash": exc.current_hash,
            },
            status_code=409,
        )
    if isinstance(exc, InvalidDocumentError):
        return JSONResponse({"detail": str(exc), "line": exc.line}, status_code=422)
    if isinstance(exc, NotFoundError):
        return JSONResponse({"detail": f"unknown project {exc.args[0]!r}"}, status_code=404)
    return JSONResponse({"detail": str(exc)}, status_code=400)


HANDLED = (
    repo.ConflictError,
    InvalidDocumentError,
    NotFoundError,
    repo.UnsafePathError,
    FileExistsError,
    FileNotFoundError,
    ValueError,
)


# ---------------------------------------------------------------- routes


@router.get("/projects")
def list_projects(request: Request) -> list[ProjectCard]:
    registry: ProjectRegistry = request.app.state.registry
    counts = registry.task_counts()
    latest = request.app.state.runs.latest_per_project()
    return [
        ProjectCard(
            id=r.id,
            name=r.name,
            path=r.path,
            status=r.status,
            priority=r.priority,
            missing=r.missing,
            errors=[DocError.model_validate(e) for e in json.loads(r.errors_json)],
            tasks_open=counts.get(r.id, {}).get("open", 0) + counts.get(r.id, {}).get("blocked", 0),
            tasks_done=counts.get(r.id, {}).get("done", 0),
            tasks_proposed=counts.get(r.id, {}).get("proposed", 0),
            last_run_at=(run := latest.get(r.id)) and (run.started_at or run.created_at),
            last_run_id=run.id if run else None,
            last_run_state=run.state if run else None,
            last_run_resolution=run.resolution if run else None,
        )
        for r in registry.rows()
    ]


@router.post(
    "/projects",
    status_code=201,
    response_model=ProjectDetail,
    responses={
        400: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
def create_project(body: CreateProjectIn, request: Request) -> ProjectDetail | JSONResponse:
    spec = repo.NewProject(
        name=body.name,
        description=body.description,
        goals=[(g.title, [a for a in g.acceptance if a.strip()]) for g in body.goals if g.title.strip()],
        constraints=[c for c in body.constraints if c.strip()],
        priority=body.priority,
        model=body.model,
        test_command=body.test_command or None,
    )
    existing = None
    if body.mode == "existing":
        if not body.path:
            return JSONResponse({"detail": "choose a folder"}, status_code=400)
        existing = Path(body.path)
    try:
        snap = _svc(request).create(spec, existing, body.git_init)
    except HANDLED as exc:
        return error_response(exc)
    except subprocess.CalledProcessError as exc:
        return JSONResponse({"detail": f"git init failed: {exc.stderr!r}"}, status_code=500)
    assert snap.meta is not None
    return _detail(snap.meta.id, snap)


@router.get(
    "/projects/{project_id}",
    response_model=ProjectDetail,
    responses={
        400: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
def get_project(project_id: str, request: Request) -> ProjectDetail | JSONResponse:
    try:
        return _detail(project_id, _svc(request).snapshot(project_id))
    except NotFoundError as exc:
        return error_response(exc)


@router.put(
    "/projects/{project_id}/docs/{doc}",
    response_model=HashOut,
    responses={
        400: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
def write_doc(project_id: str, doc: Doc, body: WriteDocIn, request: Request) -> HashOut | JSONResponse:
    try:
        return HashOut(
            hash=_svc(request).write_doc(project_id, repo.DocName(doc), body.content, body.base_hash)
        )
    except HANDLED as exc:
        return error_response(exc)


@router.patch(
    "/projects/{project_id}/settings",
    response_model=HashOut,
    responses={
        400: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
def update_settings(project_id: str, body: SettingsIn, request: Request) -> HashOut | JSONResponse:
    changes = body.model_dump(exclude={"base_hash", "clear_test_command"}, exclude_none=True)
    if body.clear_test_command:
        changes["test_command"] = None
    try:
        return HashOut(hash=_svc(request).update_settings(project_id, changes, body.base_hash))
    except HANDLED as exc:
        return error_response(exc)


@router.post(
    "/projects/{project_id}/backlog",
    status_code=201,
    response_model=HashOut,
    responses={
        400: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
def add_task(project_id: str, body: TaskIn, request: Request) -> HashOut | JSONResponse:
    try:
        h = _svc(request).add_task(
            project_id,
            body.base_hash,
            body.title,
            body.priority,
            bl.Size(body.size) if body.size else None,
            body.acceptance,
        )
        return HashOut(hash=h)
    except HANDLED as exc:
        return error_response(exc)


@router.patch(
    "/projects/{project_id}/backlog/{task_id}",
    response_model=HashOut,
    responses={
        400: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
def update_task(project_id: str, task_id: str, body: TaskPatch, request: Request) -> HashOut | JSONResponse:
    fields = body.model_dump(exclude={"base_hash"}, exclude_unset=True)
    try:
        return HashOut(hash=_svc(request).update_task(project_id, task_id, body.base_hash, **fields))
    except HANDLED as exc:
        return error_response(exc)


@router.delete(
    "/projects/{project_id}/backlog/{task_id}",
    response_model=HashOut,
    responses={
        400: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
def delete_task(
    project_id: str, task_id: str, request: Request, base_hash: Annotated[str, Query()]
) -> HashOut | JSONResponse:
    try:
        return HashOut(hash=_svc(request).delete_task(project_id, task_id, base_hash))
    except HANDLED as exc:
        return error_response(exc)


@router.post(
    "/projects/{project_id}/backlog/reorder",
    response_model=HashOut,
    responses={
        400: {"model": ErrorOut},
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    },
)
def reorder(project_id: str, body: ReorderIn, request: Request) -> HashOut | JSONResponse:
    try:
        return HashOut(hash=_svc(request).reorder_tasks(project_id, body.ids, body.base_hash))
    except HANDLED as exc:
        return error_response(exc)


@router.post("/assist/acceptance", response_model=AssistOut, responses={502: {"model": ErrorOut}})
async def assist_acceptance(body: AssistIn, request: Request) -> AssistOut | JSONResponse:
    settings = request.app.state.settings
    workdir: Path = request.app.state.paths.data_dir / "assist"
    try:
        drafts = await assist.suggest_acceptance(
            settings.claude_bin, workdir, body.name, body.description, body.goals
        )
    except (assist.AssistError, OSError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=502)
    return AssistOut(goals=[GoalIn(title=d.title, acceptance=d.acceptance) for d in drafts])


@router.get("/system")
async def system(request: Request) -> SystemOut:
    root: Path = request.app.state.paths.projects_root
    settings = request.app.state.settings
    token = has_secret(request.app.state.paths.secrets_file, CLAUDE_TOKEN)
    podman = shutil.which("podman")
    if not podman:
        return SystemOut(
            projects_root=str(root),
            podman=False,
            runner_image=False,
            token=token,
            detail="Podman is not installed; agent runs need it.",
        )
    proc = await asyncio.create_subprocess_exec(
        podman,
        "image",
        "exists",
        settings.runner_image,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    has_image = await proc.wait() == 0
    if not has_image:
        detail = "Podman found, but the runner image is missing: run `surplus runner build`."
    elif not token:
        detail = (
            "Runner image ready, but no CLAUDE_CODE_OAUTH_TOKEN in secrets.env: run `claude setup-token`."
        )
    else:
        detail = "Runner image and token ready: agent runs are possible."
    return SystemOut(projects_root=str(root), podman=True, runner_image=has_image, token=token, detail=detail)
