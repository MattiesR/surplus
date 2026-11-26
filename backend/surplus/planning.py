"""The live plan (plan §7.2): gathers the planner's inputs from the store and the collector, then calls the
pure `optimizer.planner.plan`. A thin shell: every number it passes on is logged with the decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import Clock
from surplus.collector.reset_watcher import project_reset
from surplus.collector.service import Collector
from surplus.collector.store import latest_readings
from surplus.domain.models import FIVE_HOUR, SEVEN_DAY
from surplus.optimizer.budget import Conversion, usage_trend, weekly_per_five_from_fits
from surplus.optimizer.planner import Plan, PlanInputs, anchor_drift, plan
from surplus.predictor.baseline import SlotForecast
from surplus.predictor.confidence import Confidence, ConfidenceInputs, confidence
from surplus.predictor.cost_model import CoefficientSource, CostModelFit

log = structlog.get_logger(__name__)
WEEK = timedelta(weeks=1)


@dataclass(frozen=True)
class LivePlan:
    plan: Plan | None
    unavailable: str | None  # why there is no plan
    confidence: Confidence | None = None
    conversion: Conversion | None = None
    trend: float = 1.0
    user_active: bool = False
    forecast: tuple[SlotForecast, ...] = ()  # five-hour user usage per slot until the weekly reset
    drift: timedelta | None = None  # the 5-hour reset moved off the previous grid by this much


def _fitted(fit: CostModelFit | None) -> dict[str, float] | None:
    if fit is None:
        return None
    return {
        f: c.pct_per_weighted_token
        for f, c in fit.coefficients.items()
        if c.source is CoefficientSource.FIT and c.pct_per_weighted_token > 0
    }


class PlanService:
    def __init__(self, sm: sessionmaker[Session], clock: Clock, collector: Collector) -> None:
        self.sm, self.clock, self.collector = sm, clock, collector
        self._previous: Plan | None = None

    def current(self) -> LivePlan:
        now = self.clock.now()
        with self.sm() as session:
            readings = {r.bucket: r for r in latest_readings(session)}
        five, week = readings.get(FIVE_HOUR), readings.get(SEVEN_DAY)
        missing = [
            name for name, r in (("5-hour", five), ("weekly", week)) if r is None or r.resets_at is None
        ]
        if missing or five is None or week is None or five.resets_at is None or week.resets_at is None:
            return LivePlan(None, f"no {' or '.join(missing)} reading with a reset time from Claude yet")

        m5, mw = self.collector.forecast_model(FIVE_HOUR), self.collector.forecast_model(SEVEN_DAY)
        weekly_reset = project_reset(week.resets_at, SEVEN_DAY, now)  # reported reset + k x period
        f5 = m5.forecast(now, weekly_reset)
        fw = mw.forecast(now, weekly_reset)

        fit5, fitw = self.collector.cost_model(FIVE_HOUR), self.collector.cost_model(SEVEN_DAY)
        conf = confidence(
            ConfidenceInputs(
                history_days=min(m5.history_days, mw.history_days),
                r_squared=fit5.r2 if fit5 else None,
                cost_low_confidence=fit5.low_confidence if fit5 else True,
                reading_age=now - min(five.ts, week.ts),
            )
        )
        trustworthy = bool(fit5 and fitw and not fit5.low_confidence and not fitw.low_confidence)
        conversion = weekly_per_five_from_fits(_fitted(fit5), _fitted(fitw), trustworthy)
        trend = self._trend(weekly_reset)
        activity = self.collector.activity()
        p = plan(
            PlanInputs(
                now=now,
                five_reset=five.resets_at,
                weekly_reset=weekly_reset,
                five_used=five.used_pct,
                weekly_used=week.used_pct,
                five_forecast=f5,
                weekly_forecast=fw,
                confidence=conf.value,
                user_active=activity.active,
                weekly_per_five=conversion.weekly_per_five,
                weekly_trend=trend,
            )
        )
        drift = anchor_drift(self._previous, five.resets_at)
        if drift is not None:
            log.info("plan.resynced", drift_s=drift.total_seconds(), anchor=five.resets_at.isoformat())
        self._previous = p
        log.info(
            "plan.computed",
            allowed_now=round(p.allowed_now, 2),
            weekly_surplus=round(p.weekly.surplus, 2),
            windows=len(p.windows),
            confidence=round(conf.value, 3),
            weekly_per_five=conversion.weekly_per_five,
            conversion=conversion.source,
            trend=round(trend, 2),
            user_active=activity.active,
            five_used=five.used_pct,
            weekly_used=week.used_pct,
        )
        return LivePlan(p, None, conf, conversion, trend, activity.active, tuple(f5), drift)

    def _trend(self, weekly_reset: datetime) -> float:
        """This week's user usage so far against the forecast made at the week's start."""
        week_start = weekly_reset - WEEK
        now = self.clock.now()
        if now - week_start < timedelta(hours=1):
            return 1.0
        observed = sum(
            o.manual_pct for o in self.collector.user_observations(SEVEN_DAY) if o.slot_start >= week_start
        )
        reference = self.collector.week_start_model(SEVEN_DAY, week_start)
        predicted = sum(f.p90_pct for f in reference.forecast(week_start, now))
        return usage_trend(observed, predicted)
