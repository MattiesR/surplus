"""The live plan for the dashboard timeline (plan §7.2): windows, allocations, reasons and the forecast."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from surplus.planning import PlanService

router = APIRouter(prefix="/api", tags=["plan"])


class WindowOut(BaseModel):
    start: datetime
    end: datetime
    current: bool
    manual_p90: float  # pp of the 5-hour window predicted for the user
    capacity: float  # pp an agent could use
    allocation: float  # pp planned for agents
    reason: str


class WeeklyOut(BaseModel):
    free: float
    manual_future: float
    reserve: float
    surplus: float
    planned: float  # ppw the windows' allocations add up to
    reasons: list[str]


class ForecastOut(BaseModel):
    start: datetime
    p50: float
    p90: float
    basis: str


class PlanOut(BaseModel):
    available: bool
    unavailable: str | None
    generated_at: datetime | None = None
    weekly_reset: datetime | None = None
    allowed_now: float = 0.0
    confidence: float | None = None
    confidence_reasons: list[str] = []
    weekly_per_five: float | None = None
    conversion_source: str | None = None
    conversion_reason: str | None = None
    trend: float = 1.0
    user_active: bool = False
    drift_minutes: float | None = None
    weekly: WeeklyOut | None = None
    windows: list[WindowOut] = []
    forecast: list[ForecastOut] = []


@router.get("/plan")
def get_plan(request: Request) -> PlanOut:
    svc: PlanService = request.app.state.planner
    live = svc.current()
    p = live.plan
    if p is None:
        return PlanOut(available=False, unavailable=live.unavailable)
    return PlanOut(
        available=True,
        unavailable=None,
        generated_at=p.generated_at,
        weekly_reset=p.weekly_reset,
        allowed_now=p.allowed_now,
        confidence=live.confidence.value if live.confidence else None,
        confidence_reasons=list(live.confidence.reasons) if live.confidence else [],
        weekly_per_five=p.weekly_per_five,
        conversion_source=live.conversion.source if live.conversion else None,
        conversion_reason=live.conversion.reason if live.conversion else None,
        trend=live.trend,
        user_active=live.user_active,
        drift_minutes=live.drift.total_seconds() / 60 if live.drift else None,
        weekly=WeeklyOut(
            free=p.weekly.free,
            manual_future=p.weekly.manual_future,
            reserve=p.weekly.reserve,
            surplus=p.weekly.surplus,
            planned=p.planned_weekly,
            reasons=list(p.weekly.reasons),
        ),
        windows=[
            WindowOut(
                start=w.start,
                end=w.end,
                current=w.current,
                manual_p90=w.manual_p90,
                capacity=w.capacity,
                allocation=w.allocation,
                reason=w.reason,
            )
            for w in p.windows
        ],
        forecast=[
            ForecastOut(start=f.slot_start, p50=f.p50_pct, p90=f.p90_pct, basis=str(f.basis))
            for f in live.forecast
        ],
    )
