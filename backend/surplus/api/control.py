"""Global pause, kill-switch state and the dispatcher's current decision (plan §7.3)."""

from __future__ import annotations

from datetime import datetime

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from surplus import preferences
from surplus.dispatcher import Dispatcher
from surplus.domain.events import Event
from surplus.optimizer import control

router = APIRouter(prefix="/api", tags=["control"])
log = structlog.get_logger(__name__)


class ControlOut(BaseModel):
    paused: bool
    paused_until: datetime | None
    paused_indefinitely: bool
    pause_reason: str
    user_active: bool
    activity_reason: str | None
    last_seen: datetime | None  # the user's last manual activity
    auto_start: bool  # whether an automatic run could start now (none are started before Phase 5)
    reason: str  # why or why not
    checked_at: datetime | None
    breaker_paused: list[str]  # projects the circuit breaker paused since the server started
    auto_enabled: bool  # the user's opt-in to automatic runs (ADR 0017)


class PauseIn(BaseModel):
    duration: str | None = Field(
        default=None, description="e.g. 30m, 4h or 2d; omit to pause until you resume", max_length=16
    )
    reason: str = Field(default="", max_length=200)


def _dispatcher(request: Request) -> Dispatcher:
    d: Dispatcher = request.app.state.dispatcher
    return d


def _out(d: Dispatcher) -> ControlOut:
    st = d.status
    pause, activity, decision = st.pause, st.activity, st.decision
    return ControlOut(
        paused=pause is not None,
        paused_until=pause.until if pause else None,
        paused_indefinitely=bool(pause and pause.indefinite),
        pause_reason=pause.reason if pause else "",
        user_active=bool(activity and activity.active),
        activity_reason=activity.reason if activity else None,
        last_seen=activity.last_seen if activity else None,
        auto_start=bool(decision and decision.auto_start),
        reason=decision.reason if decision else "not checked yet",
        checked_at=st.at,
        breaker_paused=list(st.tripped),
        auto_enabled=st.auto_enabled,
    )


@router.get("/control")
async def get_control(request: Request) -> ControlOut:
    d = _dispatcher(request)
    await d.tick()
    return _out(d)


@router.post("/control/pause")
async def pause(request: Request, body: PauseIn) -> ControlOut:
    d = _dispatcher(request)
    try:
        duration = control.parse_duration(body.duration) if body.duration else None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await d.pause(duration, body.reason)
    return _out(d)


class AutoIn(BaseModel):
    enabled: bool


@router.post("/control/auto")
async def set_auto(request: Request, body: AutoIn) -> ControlOut:
    d = _dispatcher(request)
    await d.set_auto(body.enabled)
    return _out(d)


@router.post("/control/resume")
async def resume(request: Request) -> ControlOut:
    d = _dispatcher(request)
    await d.resume()
    return _out(d)


class PreferencesOut(BaseModel):
    working_hours: str | None = Field(description="e.g. 'mon-fri 09:00-18:00'; null = no template")
    working_hours_text: str | None
    timezone: str


class PreferencesIn(BaseModel):
    working_hours: str | None = Field(default=None, max_length=200)


def _prefs(request: Request) -> PreferencesOut:
    hours = request.app.state.collector.working_hours()
    tz = request.app.state.settings.tz()
    return PreferencesOut(
        working_hours=hours.spec if hours else None,
        working_hours_text=hours.describe() if hours else None,
        timezone=str(getattr(tz, "key", tz)),
    )


@router.get("/preferences")
def get_preferences(request: Request) -> PreferencesOut:
    return _prefs(request)


@router.put("/preferences")
def put_preferences(request: Request, body: PreferencesIn) -> PreferencesOut:
    sm = request.app.state.collector.sm
    try:
        with sm.begin() as session:
            hours = preferences.set_working_hours(session, body.working_hours)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log.info("preferences.working_hours", spec=hours.spec if hours else None)
    request.app.state.collector.bus.publish(Event("limits.updated", {"source": "preferences"}))
    return _prefs(request)
