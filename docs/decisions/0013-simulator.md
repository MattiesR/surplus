# 0013 — The simulator: what counts as an incident, and what it assumes

- Status: accepted · Date: 2026-11-26

## Context
Plan §13 makes the simulator "the most important test in the project" and the Phase 4 DoD asks for 0
user-blocked incidents across 3 profiles × 4 weeks, with ≥ 75% utilisation for a light user. The terms needed
definitions. Implementation: `backend/surplus/simulator/`, `surplus simulate`, `tests/test_simulator.py`.

## Decision
1. **Real code, fake runner.** The seasonal baseline (refitted daily on what the simulation observed),
   confidence, activity, the planner (with the trend correction) and the hard stops are the production
   functions. Only the runner is fake: a run spends 4 pp5 per 15 min up to its budget, for at most 2 h, and is
   started only when `allowed_now >= 5 pp5` (a task needs room).
2. **Order within a step:** the agent spends before the user's demand is served (the pessimistic order). The
   kill switch acts on the next 15-min step; the real one acts within a minute, so the simulation is harsher.
3. **User-blocked incident:** the user's demand in a slot could not be fully served because a limit was at
   100%, and agents had spent in that 5-hour window (or that week). If the user alone exhausted it, that is
   reported separately ("blocked yourself"), not as an incident.
4. **Utilisation:** used percent of the weekly limit at each weekly reset (user + agents), averaged.
5. **Assumptions:** 5-hour windows on a fixed grid (as the planner projects them); 1 full window = 10% of the
   week; 3 warm-up weeks of history without agents before the 4 reported weeks; resets read without error.
6. **Profiles:** light (weekdays 09-17, ~5% a week), heavy-weekday (~65%), irregular (evenings and weekends,
   ~20%), plus a regression scenario where a light user turns heavy without warning.

## Results (2026-11-26, seed 1)
light 90% utilisation, heavy-weekday 83%, irregular 88%; 0 incidents in all, also for seeds 2-5 and for the
light-to-heavy switch (37 incidents before the trend correction of ADR 0012).
