# 0014 — Guard rails: kill switch, global pause and circuit breaker

- Status: accepted · Date: 2026-11-26

## Context
Plan §7.3 lists the hard rules the dispatcher enforces in code. Phase 4's DoD: "the kill switch pauses within
60 s of manual activity". Runs stay manual until Phase 5, so the rules must work for both kinds of run, and
the plan does not say whether a run the user started by hand is stopped by the kill switch.

## Decision
1. **One pure decision, one thin shell.** `optimizer/guard_rails.guard()` decides from (activity, pause,
   active runs, allowed_now, fresh resets, concurrency). `surplus/dispatcher.py` applies it. It runs as a
   hook right after every log ingestion (every `tick_seconds`, 60 s by default), on every pause or resume,
   and on `GET /api/control`. A stop uses the normal wrap-up path: the agent gets SIGTERM after the grace
   period, its work is committed, verified and reported, and the run can be continued later.
2. **Global pause** (`surplus pause 4h|until-resume`, `surplus resume`, `POST /api/control/pause|resume`,
   the dashboard) is stored in the `setting` table. The CLI therefore works while the server is down, and the
   server picks it up on its next tick. A pause stops *every* active run and blocks *every* start, including
   manual starts and continuations: "stop using my subscription" means all of it. It expires by itself.
3. **Kill switch.** While the user is active (the activity detector), runs with `trigger = "auto"` wrap up
   and no automatic run starts. **Runs the user started by hand keep going**: the user chose them while
   working, and stopping them would make "Run now" useless whenever a Claude Code session is open. The hard
   stops still apply to them, and a global pause stops them too. Every run records its `trigger`
   (migration 0006). Phase 5's dispatcher starts runs with `trigger = "auto"`.
4. **Automatic starts** additionally need fresh reset times, a free slot (max concurrent runs) and
   `allowed_now >= 5 pp5` (ADR 0013). In Phase 4 `allowed_now` is not passed, so the decision is always
   "no", with a reason shown on the dashboard.
5. **Circuit breaker:** when a project's 3 most recent finished runs all failed, the project is set to
   `status: paused` in its `project.md` (so the user sees it and resumes it there) and a `project.paused`
   event is published. The id of the newest failed run is stored, so resuming does not re-trip the breaker
   until another run fails. Stops by the user or the guard rails are not failures.
6. Deferred to a backlog task: quiet and holiday modes, and per-project share caps.

## Consequences
- Kill-switch latency is one tick after the log line is written: at most 60 s with the default settings,
  plus the wrap-up grace period before the container gets SIGTERM.
- `stopped_by_user`, `paused` and `user_active` are continuable exit reasons.
