"""The simulator (plan §13): a user profile through predictor, optimizer and guard rails, with a fake runner.

"This is the most important test in the project." Everything except the runner is the real code: the
seasonal baseline (refitted daily on what it observed), confidence, the activity signal, the window planner
and the hard stops. Time advances in 15-min steps:

1. resets: the 5-hour window on its grid, the week on Wednesday 20:00;
2. dispatcher: when no run is active and the user is idle, plan; start a run if `allowed_now` is big enough;
3. guard rails: the kill switch stops a run when the user became active, hard stops end it at the caps;
4. the agent spends (before the user, the conservative order), then the user's demand is served.

A **user-blocked incident** is a slot where the user's demand could not be served because a limit was at
100% *and agents had spent in that window (or week)*. Utilisation = how much of each week's limit got used.
The first `warmup_weeks` only build history (no agents) and are not reported.
"""

from __future__ import annotations

import bisect
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from surplus.clock import FakeClock
from surplus.domain.models import FIVE_HOUR, SEVEN_DAY
from surplus.optimizer.budget import BudgetConfig, usage_trend
from surplus.optimizer.planner import PlanInputs, plan
from surplus.predictor.baseline import SlotForecast, SlotObservation, fit_baseline
from surplus.predictor.confidence import ConfidenceInputs, confidence
from surplus.simulator.profiles import SLOT, Profile

# A Wednesday 20:00 UTC: the weekly reset of plan §2.3.
WEEKLY_RESET = datetime(2026, 1, 7, 20, 0, tzinfo=UTC)
EPS = 1e-9


@dataclass(frozen=True)
class SimConfig:
    weeks: int = 4
    warmup_weeks: int = 3
    seed: int = 1
    weekly_per_five: float = 0.1  # the simulated plan: a full 5-h window = 10% of the week
    five_offset: timedelta = timedelta(hours=2, minutes=30)  # first 5-h reset after the start
    run_rate: float = 4.0  # pp5 an agent spends per 15 min
    min_run_pct: float = 5.0  # a task needs room: do not start a run for less than this (pp5)
    max_run_slots: int = 8  # a run lasts at most 2 h
    budget: BudgetConfig = field(default_factory=BudgetConfig)


@dataclass(frozen=True)
class Incident:
    ts: datetime
    bucket: str
    demand: float
    served: float


@dataclass(frozen=True)
class WeekStats:
    start: datetime
    user_pct: float  # ppw
    agent_pct: float  # ppw

    @property
    def total_pct(self) -> float:
        return self.user_pct + self.agent_pct


@dataclass
class SimResult:
    profile: str
    weeks: list[WeekStats] = field(default_factory=list)
    incidents: list[Incident] = field(default_factory=list)
    self_blocked: int = 0  # the user alone exhausted a limit: not Surplus's doing
    runs: int = 0
    kill_switch_stops: int = 0
    hard_stops: int = 0

    @property
    def utilisation(self) -> float:
        return sum(w.total_pct for w in self.weeks) / (100.0 * len(self.weeks)) if self.weeks else 0.0

    def summary(self) -> str:
        return (
            f"{self.profile}: utilisation {self.utilisation:.0%} over {len(self.weeks)} weeks "
            f"(user {sum(w.user_pct for w in self.weeks) / max(1, len(self.weeks)):.0f}% + agents "
            f"{sum(w.agent_pct for w in self.weeks) / max(1, len(self.weeks)):.0f}% a week), "
            f"{len(self.incidents)} user-blocked incidents, {self.runs} runs, "
            f"{self.kill_switch_stops} kill-switch stops, "
            f"{self.self_blocked} slots where you blocked yourself"
        )


@dataclass
class _Run:
    budget: float
    spent: float = 0.0
    slots: int = 0


def simulate(profile: Profile, cfg: SimConfig = SimConfig()) -> SimResult:
    r = cfg.weekly_per_five
    start = WEEKLY_RESET - timedelta(weeks=cfg.warmup_weeks)
    eval_start = WEEKLY_RESET
    end = WEEKLY_RESET + timedelta(weeks=cfg.weeks)
    n = int((end - start) / SLOT)
    demand = profile.generate(start, n, random.Random(cfg.seed))  # noqa: S311 - synthetic data, not crypto
    clock = FakeClock(start)
    result = SimResult(profile.name)

    five_used = weekly_used = 0.0
    agent_window = agent_week = user_week = agent_week_total = 0.0
    next_five = start + cfg.five_offset
    next_week = start + timedelta(weeks=1)
    obs5: list[SlotObservation] = []
    obsw: list[SlotObservation] = []
    f5: list[SlotForecast] = []
    fw: list[SlotForecast] = []
    f_starts: list[datetime] = []
    history_days = 0.0
    week_model = None  # the weekly forecast as it was at the last weekly reset (the trend's reference)
    run: _Run | None = None
    active = False

    for i in range(n):
        t = start + i * SLOT
        clock.set(t)
        evaluating = t >= eval_start
        # ---- resets (read from the simulated "Claude", projected on its grid)
        if t >= next_five:
            five_used = agent_window = 0.0
            next_five += timedelta(hours=5)
        if t >= next_week:
            if next_week > eval_start:
                result.weeks.append(WeekStats(next_week - timedelta(weeks=1), user_week, agent_week_total))
            weekly_used = agent_week = user_week = agent_week_total = 0.0
            next_week += timedelta(weeks=1)
            week_model = fit_baseline(SEVEN_DAY, obsw, clock)
        # ---- daily refit of the predictor on what it has seen so far
        if (t.hour == 0 and t.minute == 0) or not f5:
            b5 = fit_baseline(FIVE_HOUR, obs5, clock)
            bw = fit_baseline(SEVEN_DAY, obsw, clock)
            horizon = t + timedelta(weeks=1, days=1)
            f5, fw = b5.forecast(t, horizon), bw.forecast(t, horizon)
            f_starts = [f.slot_start for f in f5]
            history_days = b5.history_days
        # ---- dispatcher (agents only after the warm-up)
        if evaluating and run is None and not active:
            k = bisect.bisect_left(f_starts, t)
            conf = confidence(ConfidenceInputs(history_days, 0.95, False, timedelta(minutes=5))).value
            week_start = next_week - timedelta(weeks=1)
            predicted_so_far = (
                sum(f.p90_pct for f in week_model.forecast(week_start, t)) if week_model else 0.0
            )
            trend = usage_trend(user_week, predicted_so_far)
            p = plan(
                PlanInputs(
                    now=t,
                    five_reset=next_five,
                    weekly_reset=next_week,
                    five_used=five_used,
                    weekly_used=weekly_used,
                    five_forecast=f5[k:],
                    weekly_forecast=fw[k:],
                    confidence=conf,
                    user_active=active,
                    weekly_per_five=r,
                    config=cfg.budget,
                    weekly_trend=trend,
                )
            )
            if p.allowed_now >= cfg.min_run_pct:
                run = _Run(budget=p.allowed_now)
                result.runs += 1
        # ---- guard rails: kill switch and hard stops
        if run is not None and active:
            run = None
            result.kill_switch_stops += 1
        if run is not None and (
            five_used >= cfg.budget.max_five_hour_pct or weekly_used >= cfg.budget.max_weekly_pct
        ):
            run = None
            result.hard_stops += 1
        # ---- the agent spends first (conservative), capped by the hard stops
        if run is not None:
            a5 = min(
                cfg.run_rate,
                run.budget - run.spent,
                cfg.budget.max_five_hour_pct - five_used,
                (cfg.budget.max_weekly_pct - weekly_used) / r,
            )
            a5 = max(0.0, a5)
            five_used += a5
            weekly_used += a5 * r
            agent_window += a5
            agent_week += a5 * r
            agent_week_total += a5 * r
            run.spent += a5
            run.slots += 1
            if run.spent >= run.budget - EPS or run.slots >= cfg.max_run_slots:
                run = None
        # ---- the user's demand
        d5 = demand[i]
        room5 = max(0.0, 100.0 - five_used)
        roomw = max(0.0, 100.0 - weekly_used) / r
        served = min(d5, room5, roomw)
        if served < d5 - 1e-6:
            bucket = FIVE_HOUR if room5 <= roomw else SEVEN_DAY
            caused = agent_window > 0 if bucket == FIVE_HOUR else agent_week > 0
            if caused and evaluating:
                result.incidents.append(Incident(t, bucket, d5, served))
            elif not caused:
                result.self_blocked += 1
        five_used += served
        weekly_used += served * r
        user_week += served * r
        obs5.append(SlotObservation(t, served))
        obsw.append(SlotObservation(t, served * r))
        active = d5 > 0  # a manual event in the last slot: the activity detector fires
    result.weeks.append(WeekStats(next_week - timedelta(weeks=1), user_week, agent_week_total))
    return result
