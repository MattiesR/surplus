# Changelog

## Unreleased — Phase 5: task selection and automatic runs · 2026-11-26

**Added**
- Task selection (`optimizer/selection.py`, plan §7.4, ADR 0016): answered questions first, then project
  priority, task priority and backlog order. Size fit (S 5 / M 15 / L 30 pp), with split mode for a large task
  in a small slot. A work-conserving 7-day fair share per project. Hypothesis: a project stays within its cap
  plus one run.
- Automatic runs (ADR 0017), **off by default**: `surplus auto on|off`, `POST /api/control/auto` and a switch
  on the dashboard. On each loop tick, when every guard rail allows it, the dispatcher selects a task and
  starts it with `trigger=auto` and a hard stop of current usage + `allowed_now`. A split-mode prompt asks
  for smaller tasks instead of changes. A refused start backs off for 30 minutes. Migration 0007 adds
  `run.mode`; the run page shows who started a run and its mode.
- Tests: opt-in, guard rails, budget, split mode, back-off, and a minute-by-minute fake-clock day (start, kill
  switch, no start while active, restart when idle).
- Inbox (ADR 0018): the open questions from run reports across projects (`GET /api/inbox`, an Inbox page with
  a count in the navigation, `g i`). Answer with an option, free text or Skip. The answer is appended to the
  project's `decisions.md` with a `T-xxx · rN Qn` reference; hand-written entries count too. The next run's
  prompt contains it, and the run page shows the exact prompt (`GET /api/runs/{id}/prompt`). Automatic runs
  wait for open questions and prefer freshly answered tasks.

## Unreleased — working hours and the plan §16 answers · 2026-11-26

**Added**
- Working-hours template (ADR 0015): `surplus hours "mon-fri 09:00-18:00"`, `GET/PUT /api/preferences` and a
  dashboard card. During cold start, the predictor stays fully conservative inside working hours and stops
  assuming the worst outside them. A changed template refits at once. The simulator has a cold-start
  regression test with a right and a wrong template.
- ADR 0015 records the user's answers to plan §16: the project's own model for automatic runs, Telegram as
  the phone channel (Phase 6), and no system packages in agent containers.

## Unreleased — Phase 4 done: confidence, activity, planner, simulator, guard rails, timeline · 2026-11-26

**Added**
- Forecast confidence (`predictor/confidence.py`): history × cost-model fit × reading freshness, each 0-1,
  with reasons. Activity detector (`predictor/activity.py`): a manual session event in the last 10 min, or an
  unexplained rise ≥ 1 pp in the last 30 min, means the user is active.
- Per-slot user usage from readings and logs (`predictor/observations.py`, ADR 0011), wired into the collector
  (`user_observations`, `forecast_model` refitted hourly over 8 weeks, `activity`). `SURPLUS_TZ` sets the time
  zone of the hour-of-week slots (default: the system's).
- Budget and window planner (`optimizer/budget.py`, `optimizer/planner.py`, plan §7.1-7.2, ADR 0012): weekly
  surplus with a confidence-scaled reserve and the end-of-week sweep, per-window capacity under the hard stops,
  windows projected from the reported resets, a back-loaded water-filling spread, reset-drift detection, and a
  weekly trend correction when the user is busier than forecast. Hypothesis: no plan exceeds a limit under P90
  user usage.
- Simulator (`surplus simulate --profile light|heavy-weekday|irregular|all --weeks N`, ADR 0013): the real
  predictor, planner and guard rails with a fake runner. 0 user-blocked incidents on all profiles and a
  light-then-heavy switch; utilisation light 90%, heavy-weekday 83%, irregular 88%. Runs in CI.
- Guard rails (`optimizer/guard_rails.py`, `surplus/dispatcher.py`, plan §7.3, ADR 0014): the kill switch
  wraps up automatic runs on the tick after manual activity (≤ 60 s); a global pause
  (`surplus pause 4h|until-resume`, `surplus resume`, `/api/control`, dashboard card) stops all runs and blocks
  starts; a circuit breaker sets a project to `paused` after 3 failed runs in a row. Runs record their
  `trigger` (manual/auto, migration 0006); `paused` and `user_active` are continuable stop reasons.
- Live plan (`surplus/planning.py`, `GET /api/plan`): the planner's inputs from the store (reported resets,
  forecasts, confidence, activity, weekly trend against the week-start forecast, the 5-hour → weekly conversion
  from both cost models or the prior), with every decision logged and reset drift re-synced.
- Plan timeline on the dashboard: one bar per 5-hour window until the weekly reset (your P90 forecast and the
  agents' planned share, hard stop line), allowed-now / weekly surplus / confidence / conversion stats, the
  reason for every window in the tooltip and a table view, and "why these numbers".

**Changed**
- The dashboard's "Next planned run" placeholder is now the "Agent runs" control card.

**Phase 4 DoD:** simulator 0 incidents across 3 profiles × 4 weeks with ≥ 75% utilisation on the light profile;
the kill switch acts within 60 s; the plan timeline is on the dashboard (vitest + Playwright). Phase 4 is done.

## Unreleased — Phase 4: seasonal baseline predictor · 2026-11-26

**Added**
- Seasonal quantile baseline (plan §6.1, ADR 0010): pure `surplus/predictor/baseline.py` fits P50/P90 of
  manual % per 15-min hour-of-week slot (local time zone) from per-slot observations, with a 3-week half-life
  decay, neighbour pooling (±1 slot at weight 0.25, wrapping around the week) and weighted empirical quantiles.
  Cold start (< 2 weeks of observed slots, or a slot with < 2 observations): P90 = the max of comparable
  time-of-day slots on any day, never below an optional working-hours template; with no comparable data the
  whole-window no-data prior applies, which leaves agents nothing. `SeasonalBaseline.forecast(start, end)`
  returns per-slot forecasts with their basis (`history`, `cold_start`, `template`, `no_data`).
- Unit tests for decay, smoothing, quantiles, time zones, cold start, template, gaps, and a hypothesis
  property (0 ≤ P50 ≤ P90 ≤ the highest observed value or prior).

## Unreleased — Phase 4: cost model · 2026-11-25

**Added**
- Cost model (plan §5.3, ADR 0009): pure `surplus/predictor/cost_model.py` builds fit windows from consecutive
  readings of a bucket plus the tokens logged in between, fits percent-per-weighted-token per model family with
  NNLS on price-ratio token weights, computes R² and flags fits with R² < 0.7 or < 8 windows as low confidence.
  Families without a positive fit fall back to the scaled price-ratio prior (never "free").
- `cost_model` table (migration `0005`) storing each fit's coefficients, weights, R², window counts and
  flag; the collector refits nightly on the last 28 days (`Collector.refit_cost_models`).
- Tests recover known coefficients from synthetic data (noise-free, noisy, and end-to-end from simulated
  readings and events); NNLS is checked against brute force.

## Unreleased — per-project runner settings and "ask to continue" · 2026-11-25

**Added**
- Per-project runner settings in `project.md` (ADR 0008): `effort` (→ `claude --effort`), `max_turns`,
  `run_timeout_minutes`, `max_five_hour_pct`, `max_weekly_pct`; editable in the Settings tab; the Run panel
  shows the effort and the project's hard stop. Runs store the limits they started with (migration `0004`).
- A run stopped by its hard stop, turn limit or time limit asks for permission to continue. "Allow and
  continue to N%" starts a continuation that resumes the same session on the same branch (fast-forward only);
  `POST /api/runs/{id}/continue`. Never automatic.
- Surplus registered as its own project (priority 1, Opus, high effort, 90% stop), with the Phase 4 backlog.
- Runner image: Node 22 LTS and uv-managed Python 3.12/3.13, so projects like Surplus itself can be verified.

## Phase 3 — Runner and sandbox · 2026-11-25

**Added**
- Phase 0 gate: spikes 3–4 completed (`docs/spikes/03-04-container-and-sandbox.md`): setup-token auth works in a
  rootless container; nested bubblewrap does not (ADR 0003 accepted); Podman's default network reaches the LAN.
- Runner image `runner-image/Containerfile` (Debian slim, git, Python, Node, uv, build tools; 730 MB), built with
  `surplus runner build` / `make runner-image`. The host's CLI binary is mounted read-only (ADR 0006).
- Isolation (`runner/container.py`): rootless Podman, agent = host uid via `--userns=keep-id` (never root),
  read-only root fs, tmpfs `/tmp` and home, `--cap-drop=all`, `no-new-privileges`, `--init`, memory/CPU/pids caps,
  self-ending lifetime; mounts: the run clone, a per-run Claude config dir and the CLI binary only.
- Network (ADR 0005): internal `surplus-agents` network + `surplus-egress` proxy (stdlib, `runner/egress_proxy.py`)
  that allows ports 80/443 to public addresses only, checks the resolved address (no DNS rebinding), refuses
  RFC1918/CGNAT/link-local/loopback/metadata/6to4/NAT64 and the host's own global addresses, logs every decision.
  Optional host allowlist `SURPLUS_EGRESS_ALLOW` (ADR 0007).
- Runs in a disposable clone (ADR 0006), git bundle snapshot first, branch `surplus/T-xxx-slug`, leftovers committed
  inside the container, branch fetched back; merge/discard from the UI only (agents never push or merge).
- Prompt composition (`runner/prompt.py`, Jinja templates in `runner/prompts/`): project, goals, task, related
  decisions, last 3 run summaries; run rules via `--append-system-prompt`.
- Stream parsing (`runner/stream.py`) → live log (SSE `run.log`), `rate_limit_event` → stored readings and a live
  hard stop (5-hour ≥ 85% or weekly ≥ 90% → the agent is asked to wrap up; work is still verified and reported).
- Verification: the project's `test_command` runs in the same container; the task is ticked off only if it passes.
- Report schema (`runner/report.py`): validated JSON, one cheap repair turn (`--resume`, Haiku, 1 turn, no tools),
  else a fallback from the branch. `.surplus/runs/<stamp>-rN.md`; suggestions appended as `proposed` tasks.
- State machine (`runner/state.py`) persisted in the new `run` table (migration `0003`); restart recovery marks
  interrupted runs and saves their branch; orphan containers of this instance are removed.
- Agent usage is ingested from each run's own session log with `is_agent`, `run_id` and `project_id`.
- API: `/api/runs` (list, detail, log, diff, preflight, start, stop, cancel, merge, discard); `/api/system` reports
  image and token presence (never the value). `surplus doctor` checks the runner.
- UI: "Run" on open backlog tasks (preflight, model, turns, remaining budget), project Runs tab, run detail page
  (live log, facts, report, test output, diff, Stop and report / Cancel / Merge / Discard).
- New projects created by Surplus get an empty first commit and a local `.surplus/` exclude.
- Tests: 181 backend (FakeRuntime end-to-end runs with real git), 7 vitest, 9 Playwright; red-team suites:
  `make redteam` (16 checks against real Podman, no quota) and `make redteam-agent` (real Haiku agent).

**DoD check**
- Red-team: the agent is instructed to write `/etc` (read-only fs), `~/` (path does not exist), reach
  `192.168.0.1` (refused by the proxy) and read `~/.ssh` (absent) → all fail, verified on the host; an internet
  fetch succeeds (example.com 200): ✅ `make redteam-agent`, plus 16 deterministic checks in `make redteam`.
- Manual run on a toy project (`toy-calc`, Haiku, 8 turns, ~1 pp of the 5-hour window): branch
  `surplus/T-001-…` with 1 commit, Surplus's own test run passed (10 tests), valid agent report, task ticked,
  2 suggestions proposed; merged from the API afterwards: ✅

**Decisions / deviations**
- ADR 0006: disposable clone instead of worktree/branch-in-place; host CLI mounted instead of baked in.
- ADR 0007: the token is readable by code inside the container (residual risk); optional egress allowlist.
- Claude Code's own sandbox stays off inside the container (ADR 0003); `bypassPermissions` only inside it.
- Prompt templates live in the package (`backend/surplus/runner/prompts/`) instead of a top-level `prompts/`.
- Answering agent questions from the UI is left to Phase 5 (questions are shown and written to the run report).

## Phase 2 — Projects and UI · 2026-11-25

**Added**
- Strict Markdown parsers/serialisers with byte-identical round trips (unit + hypothesis property tests):
  - `project.md` front matter: flat `key: value  # comment`; edits replace only the value, so comments and
    alignment survive (no YAML library, so nothing is reformatted).
  - `backlog.md`: `- [ ] T-007 (P1, ~S, proposed) Title — acceptance: …`, notes as indented lines; any malformed
    task line is an error with its line number (never silently dropped).
  - `goals.md` (sections + acceptance bullets) and `decisions.md` (append-only, enforced on write).
- Project files: atomic writes (temp + rename), every write guarded by the hash the editor started from
  (409 on conflict, never a silent overwrite); path safety checks refuse system, home and dot-config folders
  and Surplus's own data.
- Registry: discovers `<projects root>/*/.surplus/project.md` plus explicitly registered folders, indexes
  `project` and `task` tables (migration `0002`), marks vanished folders as missing.
- File watcher (`watchfiles`, 200 ms debounce; `SURPLUS_WATCH_POLL=1` forces polling) → `project.changed` SSE.
- API: list/create/detail, whole-file edits, settings, backlog add/update/delete/reorder, acceptance-criteria
  assistant (one user-triggered Haiku call; its session is logged by the CLI, so the collector counts it), system check.
- UI: router + nav + keyboard shortcuts (`g d`, `g p`, `n p`); Projects cards; Project detail with Overview
  (rendered project.md), Goals (editor), Backlog (drag or ↑/↓ to reorder, done, accept proposed, delete, add,
  or edit as text), Decisions, Runs (placeholder), Settings; 5-step new-project wizard with container check.
- Tests: 112 backend, 5 vitest, 8 Playwright.

**DoD check**
- Create a project in the UI → files on disk are correct: ✅ (`e2e/projects.spec.ts`, `test_projects_api.py`).
- Edit in an external editor → the UI updates within 2 s: ✅ (`e2e/projects.spec.ts` asserts ≤ 2 s in a real browser;
  `test_projects.py` asserts the SSE event arrives within 2 s).

**Decisions**
- Default projects root `~/surplus-projects` (`SURPLUS_PROJECTS_ROOT` overrides) until open question §16.2 is answered.
- Rendered Markdown headings are shifted down one level so each page keeps a single `<h1>`.

## Phase 1 — Skeleton and collector · 2026-11-25

**Added**
- Repo scaffold: `uv` project (FastAPI, SQLAlchemy 2 + Alembic, Typer, structlog), Vite/React/TS frontend,
  `Makefile` (`make check` = ruff + format + mypy --strict + tsc --strict + pytest + vitest), `CLAUDE.md`.
- SQLite (WAL) schema with migration `0001`: `usage_event`, `limit_reading`, `surface_reading`, `ingest_cursor`,
  `source_status`, `setting`.
- JSONL ingestion: incremental (inode + byte offset), exactly-once by global `message.id` (ADR 0001), ignores partial
  last lines and unknown line types. Real history (42 days, 78 files, 482 MB) ingests in 5.6 s; a re-run adds 0 rows.
- Percent sources: zero-quota `get_usage` probe (primary, ADR 0002), `rate_limit_event` parser (for Phase 3 runs),
  opt-in status-line spooler (`surplus statusline spool|install`), manual calibration (`POST /api/limits/manual`).
- Reset Watcher scheduling: probe every 15 min and 3 min after each expected reset; reset-shift detection (> 5 min);
  "resets stale > 1 h" flag that will pause runs.
- Manual / agent / invisible split from the probe's weekly per-surface breakdown.
- API: `/api/status`, `/api/usage/week`, `/api/probe`, `/api/events` (SSE); loopback-only, Host check (DNS rebinding),
  HttpOnly session cookie + double-submit CSRF, bearer token for the CLI.
- `surplus doctor`: CLI, auth + limits probe, Podman, log dir, percent freshness, channels.
- Dashboard: gauges with reset countdowns, cumulative stacked weekly chart with table view, split card, sources, manual
  calibration; dark-first with selected light steps; live updates over SSE.
- Tests: 55 backend (contract tests on recorded CLI fixtures, FakeClaude probe tests, hypothesis property test),
  3 vitest, 4 Playwright smoke tests (`make e2e`, isolated backend + FakeClaude).

**DoD check**
- Real history ingested idempotently: ✅
- Gauges vs `/usage`: the gauges show the CLI's own `get_usage` numbers, so they match by construction (±0 pp).
  Please confirm once against claude.ai → Settings → Usage.

**Deviations from the plan**
- React 19 instead of 18; hand-written shadcn-style components instead of the shadcn CLI (fewer deps).
- New table `surface_reading` (weekly per-surface breakdown; not in plan §4.2).
- Cost-model fitting (§5.3) is left for Phase 4, where the plan schedules it; Phase 1 splits with the price-ratio prior.

## Phase 0 — Spikes · 2026-11-25
- Findings in `docs/spikes/`, decisions in ADRs 0001–0004. Spikes 3–4 blocked on Podman + `claude setup-token`.
