# 0017 — Automatic runs: opt-in, budget and where they start

- Status: accepted · Date: 2026-11-26

## Context
Phase 5 adds runs that Surplus starts by itself: the first feature that spends quota while nobody presses a
button. Implementation: `surplus/dispatcher.py` (`loop_tick`, `_start_auto`), `surplus/preferences.py`.

## Decision
1. **Opt-in, off by default.** The user turns it on with the dashboard's "Agent runs" card,
   `POST /api/control/auto` or `surplus auto on|off`. The setting is stored in the `setting` table. While it
   is off, the guard decision says so and nothing is planned.
2. **Only the regular loop starts runs.** That is the tick after each log ingestion. A page load
   (`GET /api/control`), a pause or resume, or turning the switch on evaluates the rules but never starts a
   run, so behaviour does not depend on who has the dashboard open.
3. **Order of checks** (all must pass): opt-in, no pause, user not active (kill switch), fresh reset times,
   no active run (max 1), a live plan, `allowed_now >= 5 pp`, then task selection (ADR 0016).
4. **Budget:** an automatic run's 5-hour hard stop is lowered to *current usage + allowed_now*, never above
   the project's hard stop. The existing live enforcement stops it there, and it is continuable like any
   capped run, only when the user allows it. The weekly cap is the project's, and the planner already
   accounts for the week.
5. **Split mode:** a large task in a small slot runs with a prompt that forbids changes and asks for 2-5
   smaller tasks in `next_suggestions`. They arrive as proposals, as for any run.
6. **A refused start** (preflight problem) puts that task in a 30-minute back-off, so the loop does not hammer
   it every minute. Three failed runs in a row still pause the project (ADR 0014).
7. Runs record `trigger` (manual/auto) and `mode` (work/split, migration 0007). The run page shows both.

## Consequences
- Every automatic run still ends in "awaiting feedback": nothing merges on its own, and a task whose run waits
  for review is not started again.
- Phase 5's DoD of a week of unattended running needs the user to turn this on. Until about two weeks of
  history exist, the cold-start forecast (with the working-hours template, ADR 0015) keeps the budget small.
