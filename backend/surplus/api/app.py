"""FastAPI application: REST + SSE for the dashboard, static SPA hosting."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from surplus import __version__
from surplus.api import control as control_api
from surplus.api import projects as projects_api
from surplus.api import runs as runs_api
from surplus.api.auth import _eq, load_or_create_token, make_middleware, set_auth_cookies
from surplus.clock import Clock, SystemClock
from surplus.collector.reset_watcher import project_reset
from surplus.collector.service import Collector, ProbeFn
from surplus.config import Paths, Settings
from surplus.db.session import make_engine, make_sessionmaker, migrate
from surplus.dispatcher import Dispatcher
from surplus.domain.events import EventBus
from surplus.projects.registry import ProjectRegistry
from surplus.projects.service import ProjectService
from surplus.projects.watcher import ProjectWatcher
from surplus.runner.container import ContainerRuntime, PodmanRuntime
from surplus.runner.service import RunService
from surplus.secrets import CLAUDE_TOKEN, read_secret

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
log = structlog.get_logger(__name__)


def resolve_claude_bin(name: str) -> Path | None:
    """The real CLI binary (symlinks followed), for the read-only mount into run containers."""
    found = shutil.which(name)
    return Path(found).resolve() if found else None


class BucketOut(BaseModel):
    bucket: str
    used_pct: float
    resets_at: datetime | None
    next_reset_at: datetime | None = Field(description="resets_at rolled forward if it already passed")
    source: str
    read_at: datetime
    age_seconds: float


class SplitOut(BaseModel):
    manual: float
    agent: float
    invisible: float


class SourceOut(BaseModel):
    name: str
    ok: bool
    last_ok_at: datetime | None
    last_error_at: datetime | None
    last_error: str | None


class StatusOut(BaseModel):
    version: str
    now: datetime
    buckets: list[BucketOut]
    week_split: SplitOut | None
    resets_fresh: bool = Field(description="False → agent runs would pause (no reset time read for > 1 h)")
    sources: list[SourceOut]


class SeriesPointOut(BaseModel):
    start: datetime
    manual: float
    agent: float
    invisible: float


class ManualReadingIn(BaseModel):
    bucket: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_:\-]+$")
    used_pct: float = Field(ge=0, le=100)


def create_app(
    paths: Paths | None = None,
    settings: Settings | None = None,
    clock: Clock | None = None,
    probe: ProbeFn | None = None,
    background: bool = True,
    runtime: ContainerRuntime | None = None,
) -> FastAPI:
    paths = paths or Paths.from_env()
    settings = settings or Settings()
    clock = clock or SystemClock()
    paths.ensure()
    migrate(paths.db_url)
    engine = make_engine(paths.db_url)
    sm = make_sessionmaker(engine)
    bus = EventBus()
    collector = Collector(sm, paths, settings, clock, bus, probe)
    token = load_or_create_token(paths.api_token_file)
    registry = ProjectRegistry(sm, paths.projects_root, clock, bus)
    watcher = ProjectWatcher(registry)
    project_service = ProjectService(
        registry,
        home=Path.home(),
        protected=(paths.data_dir, paths.config_dir, paths.claude_dir),
        on_new_watch_dir=watcher.restart,
    )
    runs = RunService(
        sm,
        paths,
        settings,
        clock,
        bus,
        registry,
        project_service,
        runtime or PodmanRuntime(),
        token=lambda: read_secret(paths.secrets_file, CLAUDE_TOKEN),
        claude_bin=lambda: resolve_claude_bin(settings.claude_bin),
    )
    dispatcher = Dispatcher(sm, clock, bus, collector, runs, project_service, settings.max_concurrent_runs)
    collector.after_ingest.append(dispatcher.tick)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(registry.scan_all)
        try:
            await runs.recover()
        except Exception:
            log.exception("runs.recover_failed")
        tasks = (
            [asyncio.create_task(collector.run_forever()), asyncio.create_task(watcher.run_forever())]
            if background
            else []
        )
        try:
            yield
        finally:
            await runs.shutdown()
            for task in tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            engine.dispose()

    app = FastAPI(title="Surplus", version=__version__, lifespan=lifespan)
    app.state.collector = collector
    app.state.registry = registry
    app.state.projects = project_service
    app.state.settings = settings
    app.state.paths = paths
    app.state.runs = runs
    app.state.dispatcher = dispatcher
    app.include_router(projects_api.router)
    app.include_router(runs_api.router)
    app.include_router(control_api.router)
    allowed_hosts = frozenset(f"{h}:{settings.port}" for h in ("127.0.0.1", "localhost"))
    app.middleware("http")(make_middleware(token, allowed_hosts))

    @app.get("/auth", include_in_schema=False)
    def auth(token_param: str = Query(alias="token")) -> Response:
        if not _eq(token_param, token):
            raise HTTPException(401, "bad token")
        resp = RedirectResponse("/", status_code=303)
        set_auth_cookies(resp, token)
        return resp

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/status")
    def status() -> StatusOut:
        now = clock.now()
        split = collector.week_split()
        return StatusOut(
            version=__version__,
            now=now,
            buckets=[
                BucketOut(
                    bucket=r.bucket,
                    used_pct=r.used_pct,
                    resets_at=r.resets_at,
                    next_reset_at=None if r.resets_at is None else project_reset(r.resets_at, r.bucket, now),
                    source=r.source.value,
                    read_at=r.ts,
                    age_seconds=(now - r.ts).total_seconds(),
                )
                for r in sorted(collector.latest(), key=lambda r: (r.bucket != "five_hour", r.bucket))
            ],
            week_split=None
            if split is None
            else SplitOut(manual=split.manual, agent=split.agent, invisible=split.invisible),
            resets_fresh=collector.resets_fresh(),
            sources=[SourceOut.model_validate(s) for s in collector.source_health()],
        )

    @app.get("/api/usage/week")
    def usage_week() -> list[SeriesPointOut]:
        return [
            SeriesPointOut(start=p.start, manual=p.manual, agent=p.agent, invisible=p.invisible)
            for p in collector.week_series()
        ]

    @app.post("/api/limits/manual", status_code=201)
    def manual_reading(body: ManualReadingIn) -> BucketOut:
        r = collector.add_manual_reading(body.bucket, body.used_pct)
        return BucketOut(
            bucket=r.bucket,
            used_pct=r.used_pct,
            resets_at=None,
            next_reset_at=None,
            source=r.source.value,
            read_at=r.ts,
            age_seconds=0,
        )

    @app.post("/api/probe")
    async def probe_now() -> dict[str, object]:
        snap = await collector.probe_once()
        if snap is None:
            raise HTTPException(502, "probe failed; see sources")
        return {"readings": len(snap.readings), "subscription_type": snap.subscription_type}

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        async def stream() -> AsyncIterator[bytes]:
            async with bus.subscribe() as q:
                yield b": connected\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                    except TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield f"event: {ev.type}\ndata: {json.dumps(ev.data)}\n\n".encode()

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        if path.startswith("api/"):
            raise HTTPException(404)
        candidate = (STATIC_DIR / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(STATIC_DIR):
            return FileResponse(candidate)
        index = STATIC_DIR / "index.html"
        if index.is_file():
            return FileResponse(index)
        return Response("Frontend not built. Run `make build`.", media_type="text/plain", status_code=503)

    return app
