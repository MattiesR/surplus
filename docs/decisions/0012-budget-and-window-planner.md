# 0012 — Budget and window planner: units, conversion and spreading

- Status: accepted · Date: 2026-11-26

## Context
Plan §7.1 writes `allowed_now = min(weekly_surplus_spread, window_surplus)`, which mixes two units: percent of
the weekly limit and percent of the 5-hour window. §7.2 asks for planning per window with back-loading and an
end-of-week sweep, but leaves the exact rules open. Implementation: `optimizer/budget.py`, `optimizer/planner.py`.

## Decision
1. **Units.** Every window quantity is in pp of the 5-hour window (pp5), weekly ones in pp of the weekly limit
   (ppw). `weekly_per_five` converts: 1 pp5 of agent work costs that many ppw. It is the ratio of the two
   buckets' cost-model coefficients when both fits are trustworthy, else a **prior of 0.2** (a full window =
   20% of the week, true of small plans; larger plans make it smaller, so the prior under-spends).
2. **Window capacity** = `min(100 - used - P90 user - reserve, hard stop - used - P90 user)`, floored at 0.
   The window reserve is 25 pp5, and 100 (i.e. nothing for agents) in the current window while the user is
   active. Future windows start at 0 used.
3. **Weekly surplus** = `min(100 - used - P90 user until reset - reserve, weekly stop - used - P90 user)`.
   The reserve is `10 x (2 - confidence)`; during the last 48 h it shrinks in hourly steps to a floor of a
   quarter (the end-of-week sweep: by then the remaining user usage is small and well known).
4. **Spreading** the weekly surplus (converted to pp5) over the windows until the weekly reset: weight =
   capacity x (1 + position), position 0 for the current window and 1 for the last; water-filled up to each
   window's capacity, the excess flowing to the others. `allowed_now` is the current window's share.
5. The plan is recomputed from the newest reported resets every tick; that is the re-sync. A reported 5-hour
   reset more than 5 min off the previous grid is reported by `anchor_drift` for the decision log and UI.

6. **Trend correction** (found by the simulator, ADR 0013): the user's usage so far this week is compared with
   the P90 forecast *as it was at the weekly reset* (a refitted model already contains this week, so it would
   always look "as forecast"). If the user is busier, the rest of the week's forecast is scaled by
   `(observed + 2) / (forecast + 2)`, never below 1. Without it, a light user turning heavy lost their week to
   agents (37 blocked slots in the simulator); with it, 0.

## Consequences
- Agent work concentrates in the windows before the weekly reset; early in the week only a small share runs.
  Unspent shares roll into later windows on the next tick, never past the weekly reset.
- The property test (hypothesis) checks that under P90 user usage no window and no week goes over its
  reserve-adjusted limit or hard stop.
