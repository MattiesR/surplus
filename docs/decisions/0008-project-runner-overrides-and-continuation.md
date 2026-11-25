# 0008 — Per-project runner settings; capped runs ask before they continue

- Status: accepted · Date: 2026-11-25

## Context
The user registered Surplus itself as a project, "priority 1, opus and high effort; since it is the project to
develop itself it gets highest permissions; runs hard-stop at 90% but can ask permission to continue".
Until now every run used the global limits (5-hour stop 85%, weekly 90%, 40 turns, 45 min, no effort flag).

## Decision
1. **Per-project runner settings** in `project.md` front matter (all optional, unset = global default):
   `effort` (low…max, passed as `claude --effort`), `max_turns` (≤ 200), `run_timeout_minutes` (≤ 240),
   `max_five_hour_pct` and `max_weekly_pct` (≤ 95). Editable in the project's Settings tab. Each run stores the
   values it started with, so later edits do not change a running run.
2. **Ask, then continue.** A run that stops at its 5-hour hard stop, its turn limit or its time limit is
   still committed, tested and reported, and then waits in "needs review" with a **Continue** prompt. Only if the
   user allows it does Surplus start a *continuation*: a new run that resumes the same Claude session
   (`--resume`, the session transcript copied from the parent) on the same branch, with the 5-hour hard stop the
   user picked (up to 100%). The weekly cap stays. The branch may only move forward (fast-forward fetch); the
   parent is closed as `continued`. There is never an automatic continuation.
3. **"Highest permissions" for the Surplus project** means the most generous *runner* settings: priority 1,
   Opus, high effort, 100 turns, 120 minutes, 5-hour stop at 90%, and permission to change any part of its own
   code, including the runner and sandbox (such changes must come with an ADR and be called out in the report).
   It does **not** mean a weaker sandbox: the container, the egress proxy, the token handling, "agents never push
   or merge" and "no real quota in tests" are identical for every project. Isolation is a property of the
   runner, not a per-project permission.

## Why not a weaker sandbox for this project
Surplus's code runs on the host as the user. A change the agent makes to Surplus is harmless on its branch, but
once merged it runs with the user's full rights. The review at merge time is therefore the real security
boundary for this project, and the sandbox must still contain an agent that has been prompt-injected (for
example through a dependency's README) while it works.

## Consequences
- Opus at high effort can use a 5-hour window quickly; the 90% stop and the continue prompt keep the user in
  control of the last 10%.
- Merges of the Surplus project deserve a careful diff review, especially under `backend/surplus/runner/`.
