# 0010 — Seasonal baseline predictor: slots, smoothing and the cold-start prior

- Status: accepted · Date: 2026-11-26

## Context
Plan §6.1 asks for P50/P90 of manual % per 15-min hour-of-week slot, with a 3-week half-life decay,
weighted empirical quantiles, smoothing across neighbouring slots, and a conservative prior while there is
less than 2 weeks of data ("highest manual usage observed in any comparable slot, or a typical-working-hours
template"). Several details were open. Implementation: `backend/surplus/predictor/baseline.py` (pure).

## Decision
1. **Input** is one observation per completed 15-min slot of one bucket (manual % of that bucket). Slots with
   no usage must be passed as 0; missing slots mean "not observed" and do not count as history.
   `dense_observations` fills zeros over a covered range. The slot still running is ignored (it would
   under-report). Several observations of one slot are summed.
2. **Hour-of-week is local time** (`tz` argument, default UTC; Monday 00:00 = 0). Habits follow the wall
   clock, including across DST changes. UTC offsets are whole quarter hours, so slot boundaries agree.
3. **Decay** weight `0.5 ** (age / 3 weeks)`, age measured from `clock.now()` to the slot start.
4. **Smoothing = pooling, not averaging.** A slot's distribution pools its own samples (kernel weight 1) and
   those of the slots ±1 away (weight 0.25 each; the week wraps around). Averaging neighbouring quantiles
   would halve a spike's P90. With pooling, each neighbour has 1/6 of the weight, which is above the 10% tail,
   so a spike also sets its neighbours' P90 (start-time jitter is covered). Neighbours together weigh less
   than the slot itself, so a consistent value keeps its own P50.
5. **Quantiles** use the inverted weighted CDF (smallest value whose cumulative weight reaches q), so every
   forecast is a value that actually happened.
6. **Cold start** applies globally when fewer than 2 weeks of *observed* slots exist, and per slot when the slot
   itself has fewer than 2 observations (gaps in an otherwise warm model). Then:
   - P90 = the max manual % of any *comparable* slot = the same time of day ± the neighbour radius, on **any**
     day of the week (with one week of data, weekday/weekend distinctions are not yet trustworthy);
   - P50 = weighted median of the same pool, capped at P90;
   - if a template is given, P90 is never below it, and it replaces the no-data prior.
7. **No-data prior:** a slot with no comparable observation and no template gets `100 / slots per bucket
   period` for both P50 and P90 (5% per slot for the 5-h bucket, 100/672% for weekly). Summed over a window
   that is 100%: the user is assumed able to use the whole window, so agents get nothing there until data
   arrives.
8. **Template** (onboarding is not built yet) is a P90 prior per hour-of-week slot of one bucket. It only takes
   effect during cold start and never lowers the prior below observed data.

## Consequences
- The first days are very conservative: unobserved times of day block agent work. That is intended (plan §1:
  "conservative under uncertainty").
- Old data from long ago still counts as history (quantiles are relative to the total weight); data freshness
  belongs in the forecast confidence (§6.4), not here.
- Wiring (deriving per-slot manual % from the store, storing `forecast` rows) is a separate step.
