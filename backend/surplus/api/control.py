"""Global pause, kill-switch state and the dispatcher's current decision (plan §7.3)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from surplus.dispatcher import Dispatcher
from surplus.optimizer import control

router = APIRouter(prefix="/api", tags=["control"])


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


@router.post("/control/resume")
async def resume(request: Request) -> ControlOut:
    d = _dispatcher(request)
    await d.resume()
    return _out(d)
