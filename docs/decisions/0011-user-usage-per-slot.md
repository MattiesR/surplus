# 0011 — What the predictor learns from: per-slot *user* usage

- Status: accepted · Date: 2026-11-26

## Context
The seasonal baseline (ADR 0010) needs one observation per completed 15-min slot of a bucket: how much of the
limit *the user* used. The store has percent readings (probe every ~15 min, the stream during runs) and
logged token events (manual from the host's logs, agent from run logs). Usage on other surfaces (claude.ai,
desktop app, other machines) appears only in the readings.

## Decision
1. **User usage = everything that is not a Surplus agent**: manual Claude Code use plus invisible usage. Both
   compete with agents for the same limit, so both must be forecast.
2. For each pair of consecutive readings in the same window (reported resets within 5 min, at most 6 h apart,
   not decreasing), manual events add their cost-model estimate to their slot, and the rise the logs do not
   explain (`rise - manual - agent`, if positive) is spread over the interval's slots by overlap.
3. **No cost model → nothing is explained away**: the whole rise counts as the user, agent spend included.
   When the cost model over-estimates, manual usage is still taken at its estimate. Both err towards a busier
   user, i.e. fewer agent runs.
4. **Observed** = a completed slot at least half covered by such reading pairs. Logs from before Surplus took
   readings are *not* used as history: they would miss invisible usage and under-state the user.
5. Hour-of-week uses the system time zone (`/etc/localtime`), overridable with `SURPLUS_TZ`.
6. The baseline is fitted on 8 weeks of observations and refitted at most hourly, in memory. Persisting
   forecasts (`forecast` table) is deferred to the backtest work of plan §6.2, its only consumer.

## Consequences
- History starts when Surplus starts reading limits; the first two weeks run on the cold-start prior.
- Gaps (PC off, probe failures) are not zeros, so they do not make the user look idle.
