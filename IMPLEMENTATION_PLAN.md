# Surplus — Implementation Plan

> **Surplus** (working name) puts the unused capacity of your AI subscriptions to work. It learns how you use Claude yourself, predicts how much of the 5-hour and weekly limits you will leave unused, and spends that spare capacity on your own side projects. It runs agents in sandboxes, reports back after each run, and asks you what to build next.

This document is the build spec for Claude Code. Read it in full before writing code. Build **phase by phase** and meet every acceptance criterion before moving to the next phase.

---

## 0. How to use this document (instructions for Claude Code)

1. Start with **Phase 0 (spikes)**. Several design decisions depend on facts about the Claude Code CLI that must be checked on this machine, not assumed. Record the findings in `docs/spikes/*.md`, and update this plan if a finding contradicts it.
2. Keep `CLAUDE.md` at the repo root up to date with the conventions in §13.
3. Every phase ends with passing tests, `make check` green, and a short `docs/changelog.md` entry.
4. When this plan is ambiguous, choose the **safer** option (less quota spent, fewer permissions, more user control) and note the decision in `docs/decisions/NNNN-title.md` (ADR format).
5. Never read, copy, or send subscription login tokens anywhere except to the official CLI that owns them (see §9.4).

---

## 1. Goals and non-goals

### 1.1 Goals
| # | Goal | Measured by |
|---|------|-------------|
| G1 | Your own work is never blocked by limits that Surplus caused | 0 incidents/month of the user hitting a limit while background spend was > 0 in that window |
| G2 | Use the capacity you are already paying for | Weekly utilisation rises from the baseline (say 30–40%) to 80–90% |
| G3 | Autonomous output you can actually use | ≥ 60% of agent runs end in a report the user marks 👍 or merges |
| G4 | Low-friction control from the phone | User can approve, answer, or pause from the phone in < 30 s |
| G5 | Safe by construction | An agent cannot write outside its project folder or read host secrets |

### 1.2 Non-goals (v1)
- Multi-user or SaaS hosting. This is a **single-user, local-first** app.
- Proxying or reselling subscription access, or calling model APIs directly with subscription tokens.
- Fully autonomous merging to `main`. Agents work on branches and a human merges.
- Perfect prediction. The system must be **conservative under uncertainty**, not clever.

---

## 2. Key domain facts and constraints

These facts shape the whole architecture. Verify each one in Phase 0.

1. **Limits are opaque and measured in percent, not tokens.** Subscription plans expose usage as "% of the 5-hour window used" and "% of the weekly limit used", plus reset times. The token-to-percent ratio is not published and depends on the model (Opus costs more than Sonnet), on caching, and on context size.
   → Surplus's **internal currency is "% of limit"**. Token counts are *features* used to estimate percent, never the budget itself.
2. **Web, desktop, mobile and Claude Code share the same limits.** Usage in the claude.ai chat UI does **not** appear in local Claude Code logs.
   → The collector must estimate "invisible" usage from changes in the authoritative percent readings that the logs do not explain.
3. **Reset times are read from the subscription, never computed.** The plan settings page (claude.ai → Settings → Usage) and the CLI show each bucket's `used %` and `resets_at`. Example observed on 2026-11-25: *5-hour limit 3%, resets Thu 01:30*; *Weekly · all models 0%, resets Wed 20:00*. Surplus treats the reported `resets_at` as the **only** source of truth for window boundaries. Scheduling is aligned to the subscription's own windows, and Surplus never assumes or invents its own.
   - **No reset time is hard-coded or typed in by the user.** A dedicated **Reset Watcher** (§5.1, source 2) asks Claude for the current reset times and stores every answer. All schedule decisions use the latest reset times Claude reported.
   - **Weekly:** the weekly reset (observed today: Wednesday 20:00) is *learned* from the watcher's readings, used to project future weeks, and replaced as soon as Claude reports something different.
   - **5-hour:** windows are **fixed** by the subscription and do not depend on when the user first sends a message. The current window's end comes straight from Claude's reported `resets_at`. Windows further ahead are projected as `last reported reset + k × 5 h`, and each projection is replaced by Claude's real value once that window becomes current. Surplus never tries to move or start windows itself.
4. **Several limit buckets can exist** (for example all-models and a model-specific weekly cap). Model them as a list of `LimitBucket`s, each with its own `resets_at`, never as two hard-coded numbers.
5. **Headless runs are supported:** `claude -p` with `--output-format stream-json`, `--max-turns`, `--model`, `--allowedTools`, `--permission-mode`, and `--append-system-prompt`. For long-lived non-interactive authentication, `claude setup-token` provides an official subscription token for headless/CI use (`CLAUDE_CODE_OAUTH_TOKEN`).
6. **Claude Code has a built-in sandbox** (bubblewrap on Linux) that limits filesystem writes to the working directory and can control network access. Surplus adds a **container** on top for defence in depth (§9).

---

## 3. System architecture

```
┌──────────────────────────────── Host (Linux, systemd --user) ────────────────────────────────┐
│                                                                                               │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐                    │
│  │  Collector   │──▶│  Usage Store │◀──│  Predictor   │──▶│  Optimizer   │                    │
│  │ (log tailer, │   │  (SQLite)    │   │ (per-slot    │   │ (budget +    │                    │
│  │  % probes)   │   │              │   │  quantiles)  │   │  plan)       │                    │
│  └──────────────┘   └──────┬───────┘   └──────────────┘   └──────┬───────┘                    │
│                            │                                     │ plan                       │
│  ┌──────────────┐   ┌──────▼───────┐   ┌──────────────┐   ┌──────▼───────┐   ┌─────────────┐  │
│  │  Web UI      │◀─▶│  API (REST + │◀─▶│  Project     │◀─▶│  Dispatcher  │──▶│  Runner     │  │
│  │ (React SPA)  │   │  SSE)        │   │  Registry    │   │ (guard rails,│   │ (container  │  │
│  └──────────────┘   └──────┬───────┘   │ (md files)   │   │  kill switch)│   │  + claude   │  │
│                            │           └──────────────┘   └──────────────┘   │  -p)        │  │
│  ┌──────────────┐   ┌──────▼───────┐                                         └──────┬──────┘  │
│  │ Notifier     │◀──│  Event Bus   │◀──────────── run events / reports ─────────────┘         │
│  │ (email, TG,  │   │ (in-process) │                                                          │
│  │  ntfy, WA)   │──▶│  inbound     │   phone replies → feedback → project files               │
│  └──────────────┘   └──────────────┘                                                          │
└───────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Process model:** one Python process (`surplusd`) holds the API, the scheduler loop and the notifier. Agent runs are **child containers**, so the daemon keeps running when an agent crashes. Everything lives under `~/.local/share/surplus/` (data) and `~/.config/surplus/` (config).

### 3.1 Tech stack
| Layer | Choice | Why |
|-------|--------|-----|
| Language (backend) | Python 3.12+, managed with `uv` | Good ecosystem for forecasting and scripting |
| API | FastAPI + Pydantic v2, SSE for live updates | Typed, fast, OpenAPI is generated for the frontend |
| DB | SQLite (WAL mode) via SQLAlchemy 2 + Alembic | Zero-ops, enough for a single user, migrations from day one |
| Scheduling | Own asyncio tick loop (every 60 s) + APScheduler for cron-like jobs | The optimizer needs a custom loop; cron jobs are boring and well solved |
| Forecasting | numpy/pandas; v2: `scikit-learn` quantile gradient boosting | Start simple and explainable |
| Frontend | React 18 + Vite + TypeScript, TanStack Query, Tailwind + shadcn/ui, Recharts, a Markdown editor (`@uiw/react-md-editor` or CodeMirror 6) | Professional look quickly, typed API client generated from OpenAPI (`openapi-typescript`) |
| Sandbox | Rootless **Podman** (Docker-compatible fallback) + Claude Code's built-in sandbox inside | Two layers; rootless means a container escape is not root |
| Packaging | `surplus` CLI (Typer); systemd user unit; the frontend is built into static files served by FastAPI | One command to install, one service to run |
| Quality | ruff, mypy --strict, pytest (+ hypothesis for the optimizer), Vitest + Playwright for the UI, pre-commit | "S-tier" means it is enforced, not aspirational |

---

## 4. Data model

### 4.1 Filesystem: projects are folders, Markdown is the source of truth for intent

```
~/surplus-projects/                       # configurable root
  my-cool-idea/
    .surplus/
      project.md        # name, one-paragraph description, tech constraints (front-matter + prose)
      goals.md          # goals, ordered, each with acceptance criteria
      backlog.md        # tasks (agent-proposed + user-written), checkbox list with IDs
      decisions.md      # answers the user gave to agent questions (append-only)
      runs/
        2026-11-26T0312Z-r42.md   # human-readable report for each run
    ... project source code (git repo) ...
```

`project.md` front-matter example:
```yaml
---
id: my-cool-idea
name: My Cool Idea
status: active          # active | paused | archived
priority: 2             # 1 = highest
model: sonnet           # default model for runs; opus allowed only if a budget flag permits
max_share_of_surplus: 0.5   # this project may use at most 50% of surplus capacity
test_command: "uv run pytest -q"
---
```

**Rules**
- The UI edits these files. The DB **indexes** them (mtime and hash) but never becomes the authority for intent. A file watcher (`watchfiles`) re-indexes on external edits, so you can also edit them in your own editor.
- Backlog item syntax: `- [ ] T-007 (P1, ~S) Add CSV export — acceptance: …`. The parser is strict and has round-trip tests (parse → serialise → identical bytes).

### 4.2 SQLite tables (core)

| Table | Key columns | Notes |
|-------|-------------|-------|
| `usage_event` | id, ts, source (`cc_log`/`agent`/`probe`), session_id, project_id?, model, input_tok, output_tok, cache_create_tok, cache_read_tok, is_agent | One row per assistant message in the logs; deduplicated by the global `message_id` (amended by ADR 0001: resumed sessions copy history across session files) |
| `limit_reading` | ts, bucket (`five_hour`, `weekly_all`, `weekly_<model>`…), used_pct, resets_at, source | Authoritative percent readings |
| `window` | id, bucket, started_at, resets_at, peak_pct, agent_pct_est, manual_pct_est | Derived; one row per observed 5-h window or week |
| `cost_model` | model, ts_fitted, pct_per_weighted_token, weights_json, r2 | Calibration from tokens to percent (§5.3) |
| `forecast` | generated_at, slot_start, bucket, p50_pct, p90_pct | Predicted *manual* usage per 15-min slot |
| `plan` | generated_at, slot_start, allowed_agent_pct, reason | Optimizer output |
| `project` | id, path, status, priority, content_hash | Index of the md files |
| `task` | id, project_id, backlog_id, state, est_size | Mirror of the backlog plus state |
| `run` | id, project_id, task_id, state, started_at, ended_at, model, budget_pct, spent_pct_est, exit_reason, branch, report_path | State machine in §7.2 |
| `message` | id, run_id?, channel, direction (`out`/`in`), payload, ts, correlation_id | Notifications and phone replies |
| `setting` | key, value_json | Reserve %, quiet hours, channels… |

---

## 5. Collector

### 5.1 Sources (each one is a plugin behind a `UsageSource` interface)
1. **Claude Code JSONL logs** — tail `~/.claude/projects/**/*.jsonl` (and the container-mounted config dirs used by agent runs). Parse assistant messages with `usage` fields. Keep an incremental cursor (file inode + byte offset) for exactly-once ingestion.
2. **Authoritative limit percentages** — the source of truth for budgets. Candidates to evaluate in **Phase 0**, in order of preference:
   a. The JSON that Claude Code passes to a **status line command** (recent versions include rate-limit information: used percent and reset time per bucket). Surplus installs a tiny status-line script that writes the JSON to a spool file. This is passive, and free whenever you use Claude Code yourself.
   b. **Rate-limit events in `stream-json` output** from agent runs.
   c. A **cheap probe**: run `/usage` (or the equivalent) non-interactively and parse the output, if the CLI supports it without spending quota.
   d. **Manual calibration** in the UI ("I'm at 34% weekly"). This is a fallback, and also useful for validation.
   > **Amended (ADR 0002, spike 2):** source **c** exists and costs no quota: the `get_usage` control request over `--input-format stream-json` returns every bucket's percent and `resets_at`. It is now the **primary** source; the status line (a) is opt-in because it edits the user's Claude settings.

   Each reading is stored with its `source`. The system keeps working with any non-empty subset of sources, but at lower confidence (§6.4).

   **Reset Watcher** (part of this source): it reads both `used %` and `resets_at` for every bucket
   - every 15 minutes (only through sources that cost no quota),
   - right before and after every agent run,
   - a few minutes after each expected reset, to confirm that the reset happened and to pick up the next reset time.

   If a reported reset time is more than 5 minutes off what Surplus expected, it replaces the expectation, redoes the schedule, and shows the change in the dashboard. If no fresh reset time could be read for more than 1 hour, agent runs **pause** until a fresh reading arrives, because the schedule would otherwise run on guesses.
3. **Agent run telemetry** — the runner tags every event with `run_id`/`project_id` and `is_agent=true`.
4. *(v2)* **Codex CLI / Gemini CLI** — same interface, separate `LimitBucket`s per provider.

### 5.2 Classification: manual vs agent vs invisible
- `agent` = events from Surplus runs (exact, by tag).
- `manual_visible` = all other Claude Code log events.
- `manual_invisible` = Δ(authoritative %) − estimated % from visible events, clamped at ≥ 0. This captures claude.ai web, desktop and mobile chat usage.

### 5.3 Cost model (tokens → % of limit)
Fit a regression per model and bucket on windows that have both log data and percent readings:
`pct ≈ Σ_m a_m · (w_in·in + w_out·out + w_cc·cache_create + w_cr·cache_read)_m`
- Start with fixed weights taken from the API price ratios as a prior, then fit `a_m` using non-negative least squares. Refit nightly and store the R² value. **If R² < 0.7, widen the safety margins** (§6.4).
- Unit-test it with synthetic data that has known coefficients.

### 5.4 Acceptance criteria
- Ingesting 30 days of real logs takes < 10 s, is idempotent (a re-run adds 0 rows), and survives a truncated last line.
- The dashboard shows: current 5-h %, current weekly %, reset countdowns, and the split between manual, agent and invisible usage.

---

## 6. Predictor

**Question it answers:** *"For each 15-minute slot from now until the weekly reset, how much of each limit bucket will **I myself** use?"*, as a distribution, not a point estimate.

### 6.1 v1 — seasonal quantile baseline (ship this first)
- Features: hour-of-week (672 slots of 15 min), with exponential decay across weeks (half-life 3 weeks, so the model adapts when your habits change).
- Output: P50 and P90 of manual % per slot, computed with weighted empirical quantiles and smoothed across neighbouring slots.
- Cold start (< 2 weeks of data): use a conservative prior. Assume P90 = the highest manual usage observed in any comparable slot, or a user-set "typical working hours" template from onboarding.

### 6.2 v2 — contextual model
- Gradient-boosted quantile regression with these features: hour-of-week, holiday flag, usage in the last 1/4/24 h, the week's usage so far versus normal, and an optional calendar busy/free signal (ICS URL).
- Promote v2 only if it beats v1 on **backtest pinball loss** over the last 4 weeks (keep an automated backtest report in the UI).

### 6.3 "Am I working right now?" detector
A real-time signal, separate from the forecast: a manual Claude Code session event in the last N minutes, or a jump in invisible percent → **user active**. This feeds the kill switch (§7.3).

### 6.4 Confidence
Each forecast has a `confidence ∈ [0,1]` based on the amount of history, cost-model R², and the freshness of authoritative readings. Low confidence means bigger reserves (§7.1). This is how the system stays safe when it does not know.

---

## 7. Optimizer and dispatcher

### 7.1 Budget computation (every tick, per bucket)
```
weekly_free     = 100% − weekly_used_now
manual_future   = Σ P90(manual) over slots until weekly reset
reserve_weekly  = base_reserve (default 10%) × (2 − confidence)
weekly_surplus  = max(0, weekly_free − manual_future − reserve_weekly)

window_free     = 100% − five_hour_used_now
manual_window   = Σ P90(manual) over the remaining slots of the current 5-h window
reserve_window  = user_active ? 100% : base_window_reserve (default 25%)
window_surplus  = max(0, window_free − manual_window − reserve_window)

allowed_now     = min(weekly_surplus_spread, window_surplus)
```
`weekly_surplus_spread` spreads the weekly surplus over the low-manual windows left before the reset, so the agent doesn't burn the whole week on Monday night. Spending is **back-loaded** as the reset approaches, because uncertainty drops.

### 7.2 Window planner (aligned to the subscription's windows)
Plan per **subscription window**, not per minute:
1. Take the current window boundaries from the Reset Watcher's latest reading (`resets_at` per bucket). Project further 5-h windows as `last reported reset + k × 5 h`, and further weeks from the learned weekly reset (§2.3).
2. For each projected window, compute free capacity = 100% − predicted P90 manual usage − reserve. Place agent runs in windows with spare capacity, with the most weight on the windows just before a reset: capacity that is unused when a window resets is lost, so the final hours before a reset are the cheapest to spend.
3. **Weekly end-of-week sweep:** in the last 24–48 h before the Wednesday 20:00 reset, the weekly reserve shrinks step by step (uncertainty about your remaining usage is small by then), so leftover weekly capacity is spent rather than lost.
4. Choose greedily with a local search (no solver needed). A property test (hypothesis) checks that **no plan exceeds a limit under P90 manual usage**.
5. When a new reading reports a `resets_at` that differs from the projected grid by more than 5 minutes, the anchor is re-synced, the plan is recomputed immediately, and the difference is logged and shown in the UI.

The plan is shown in the UI as a timeline: your predicted usage, planned agent blocks, window boundaries, and the reason for each decision.

### 7.3 Dispatcher guard rails (the hard rules, enforced in code, not in the plan)
- **Kill switch:** user active → pause new runs immediately. A running agent is told to wrap up: send SIGTERM to the container after a grace period, which lets it commit work in progress.
- **Hard stop:** stop at `allowed_now` or when the fresh 5-h % passes the threshold (whichever comes first). Stream-json rate-limit events are checked live.
- **Global pause** from the UI, phone, or the CLI (`surplus pause 4h`).
- **Quiet and holiday modes**, a maximum number of concurrent runs (default 1), and per-project share caps.
- **Circuit breaker:** after 3 failed runs on a project, pause that project and notify the user.

### 7.4 Task selection
Choose by priority: answered questions that unblock a task first, then by project priority × goal order × task size fit. A large task does not start in a small slot; the agent is asked to split it instead. The fair-share rule per project is enforced over a 7-day rolling window.

### 7.5 Run state machine
`queued → preparing (container, branch) → running → wrapping_up → verifying (tests) → reporting → awaiting_feedback → done`
Side exits: `paused_by_user`, `budget_exhausted`, `failed`, `cancelled`. Every transition is persisted, so the daemon can restart **mid-run** and recover: re-attach to the container or mark the run interrupted.

---

## 8. Runner (one agent run)

1. **Prepare:** `git worktree` or a branch `surplus/T-007-csv-export` in the project repo; refuse if the working tree is dirty with user changes.
2. **Compose the prompt** (template in `prompts/run.md.j2`): project.md, goals.md, the selected task, the relevant decisions.md entries, a summary of the last 3 run reports, plus **run rules**: stay in scope, commit often with conventional commits, write tests, stop and ask when uncertain, and end with the required report JSON.
3. **Execute** in the container (§9):
   `claude -p "<prompt>" --output-format stream-json --verbose --model <m> --max-turns <n> --permission-mode <mode> --append-system-prompt <rules>`
   Stream events → `usage_event` + SSE to the UI (live log view).
4. **Verify:** run the project's `test_command` in the same container; record the result.
5. **Report:** the agent's final message must contain a JSON block validated against a schema:
   ```json
   {"summary": "...", "done": ["..."], "not_done": ["..."], "tests": "pass|fail|none",
    "questions": [{"id":"Q1","text":"...","options":["A ...","B ..."],"recommended":"A"}],
    "next_suggestions": [{"title":"...","why":"...","size":"S|M|L"}]}
   ```
   If the JSON is invalid → one cheap repair turn → otherwise a fallback report generated from the git diff. Surplus writes `runs/<id>.md`, updates backlog.md (it ticks the task off only if tests pass), appends the suggestions as `proposed` items, and emits a `run.reported` event.
6. **Never** push to a remote or merge. The user merges from the UI ("Merge branch", "Discard", "Ask agent to revise").

---

## 9. Sandbox and security

### 9.1 Threat model (what this protects against)
- An agent, possibly prompt-injected by web content, tries to: write outside the project, read `~/.ssh` or browser data or other credentials, delete the repo history, or mine crypto and burn quota.
- A bug in Surplus that spends more quota than allowed.

### 9.2 Container per run
- Rootless Podman, image `surplus-runner` (Debian slim + Node + Claude Code CLI + git + common toolchains; per-project `Containerfile` overrides are allowed).
- **Mounts:** the project folder at `/workspace` (rw) **only**. No home directory, no Docker socket. Claude Code config goes in a **dedicated per-run config dir** (rw, on a tmpfs or in the run folder), so logs are captured and the host `~/.claude` is not exposed.
- **Network:** outbound internet allowed (a requirement); no access to the host network namespace; block LAN ranges (RFC1918, link-local, the host gateway) with a nftables rule or Podman network options, so an agent cannot reach your router, NAS or the Surplus API.
- **Limits:** CPU/memory/pids caps, a wall-clock timeout, read-only root filesystem except `/workspace` and `/tmp`.
- Inside: Claude Code's own sandbox is enabled as well (filesystem writes restricted to `/workspace`), with `--dangerously-skip-permissions` allowed **only** inside this container, never on the host.
- **Git safety:** Surplus takes a `git bundle` snapshot before each run, so even a `rm -rf .git` can be undone.

### 9.3 Surplus itself
- The API binds to `127.0.0.1` only. Phone access goes through **Tailscale** (free tier) or the messaging bots (§10), never through a public port.
- A local auth token is still required on the API (protection against DNS rebinding and malicious local pages): an HTTP-only cookie plus a CSRF token.
- Secrets (SMTP password, bot tokens) live in `~/.config/surplus/secrets.env` (chmod 600) or the OS keyring (`keyring` lib), are never logged, and are never mounted into containers.

### 9.4 Authentication of the CLI inside the container, and terms of service
- Use the official mechanism for headless subscription use (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`), injected as an environment variable into the container only. Verify in Phase 0.
- Surplus **only drives the official CLIs**. It never reads, extracts, or uses subscription tokens in its own HTTP calls, and never sends them to third parties. Keep a `docs/compliance.md` that the user re-checks against the current terms of each provider.

---

## 10. Notifications and phone feedback

### 10.1 Channel comparison (cost-free options)
| Channel | Cost | Two-way | Rich replies (buttons) | Verdict |
|---------|------|---------|------------------------|---------|
| **Telegram bot** | Free, unlimited | ✅ (long-polling, no public URL needed) | ✅ inline keyboards | **Primary phone channel.** Best free two-way option. |
| **Email (SMTP + IMAP)** | Free (e.g. Gmail app password) | ✅ reply parsing via IMAP | ❌ (links only) | **Primary for digests**, secondary for replies |
| **ntfy.sh** (or self-hosted) | Free | ⚠️ action buttons → HTTP callbacks (needs Tailscale) | ✅ simple actions | Good for push alerts |
| **WhatsApp – Meta Cloud API** | Some service conversations are free; business-initiated template messages are generally **paid** | ✅ webhook (needs a public HTTPS URL) | ✅ | Not cost-free for proactive alerts; complex setup |
| **WhatsApp – CallMeBot** | Free for personal use | ❌ outbound only, unofficial | ❌ | Optional "ping me" only |

**Decision:** build a `Channel` interface with an **Email** and a **Telegram** adapter in v1, and **ntfy** in v1.1. WhatsApp via CallMeBot is optional, outbound only; Meta Cloud API is behind a flag with a cost warning. Check current WhatsApp pricing at implementation time, because it changes.

### 10.2 Message types
- `run.reported`: summary, tests, diff stats, buttons: **👍 Good / 👎 Bad / Merge / Revise**, and each question rendered with its options, "Recommended" marked.
- `question.pending` reminder (at most 1 per day per project).
- `budget.daily_digest` (opt-in, sent at a chosen time): yesterday's agent work, the week's utilisation, the forecast.
- `alert`: circuit breaker tripped, a source went stale (> 24 h without percent readings), container failures.

### 10.3 Inbound (phone → Surplus)
- Telegram callback data and email replies carry a `correlation_id`. Free-text replies map to: an answer to Qn → appended to `decisions.md`; "revise: …" → a new task linked to the run; commands `/pause 3h`, `/resume`, `/status`, `/next <project>`.
- **Authorisation:** only the paired Telegram chat ID / allow-listed sender address. Pair with a one-time code shown in the UI. Messages from anyone else are ignored and logged.
- Anything that changes code (merge) needs a **second confirmation** in the UI, or a confirm button with a 5-minute expiry.

---

## 11. Web UI

Design language: clean, dense, dark-mode-first (shadcn/ui), keyboard shortcuts, live updates over SSE.

| Screen | Contents |
|--------|----------|
| **Dashboard** | Two big gauges (5-h %, weekly %) with reset countdowns; stacked area chart manual / invisible / agent over the week; "Next planned run" card; global Pause button; "Surplus this week: est. X%" |
| **Plan timeline** | 48-h timeline: forecast band (P50–P90), window boundaries, planned agent blocks; hover shows the reason; drag to block out "I'll be working" times |
| **Projects** | Cards (status, priority, last run, open questions badge) → **Project detail** with tabs: Overview (rendered project.md), Goals (editor), Backlog (drag-to-reorder, edits backlog.md), Decisions, Runs, Settings (model, share cap, test command) |
| **New project wizard** | Name → description → goals (with an optional "help me write acceptance criteria" button that uses one small Claude call) → choose an existing folder or `git init` a new one → container image check |
| **Run detail** | Live streamed log, tokens/% spent versus budget, diff viewer, test output, report, feedback buttons |
| **Inbox** | All pending questions and suggestions across projects; answer inline |
| **Insights** | Predictor accuracy (backtest), cost-model fit, utilisation history, per-project spend |
| **Settings** | Reserves, quiet hours, channels + pairing, sources health, data export |

Accessibility: WCAG AA contrast, all controls usable from the keyboard. Playwright smoke tests for each screen.

---

## 12. Repository layout

```
surplus/
  CLAUDE.md
  Makefile                 # check, test, dev, build, install
  pyproject.toml           # uv
  backend/surplus/
    api/                   # FastAPI routers, SSE
    collector/             # sources/, parser, cost_model.py
    predictor/             # baseline.py, gbm.py, backtest.py
    optimizer/             # budget.py, planner.py, dispatcher.py
    runner/                # container.py, prompt.py, stream.py, report.py
    projects/              # md parsing/serialising, watcher
    notify/                # channel.py, email.py, telegram.py, ntfy.py
    domain/                # pydantic models, state machine, events
    db/                    # SQLAlchemy models, alembic/
    cli.py                 # Typer: surplus serve|pause|status|install|doctor
  backend/tests/           # unit, property (hypothesis), integration, fixtures/logs/
  frontend/                # Vite React TS
  runner-image/Containerfile
  prompts/                 # run.md.j2, report.schema.json
  deploy/surplus.service   # systemd --user unit
  docs/ (spikes/, decisions/, compliance.md, changelog.md)
```

---

## 13. Engineering standards (copy into CLAUDE.md)

- **Typed everywhere:** `mypy --strict` and `tsc --strict`; Pydantic models at every boundary.
- **Pure core, thin shell:** predictor, budget, planner and the state machine are pure functions with **no I/O**, so they are exhaustively testable. I/O lives in adapters.
- **Fake time:** every time-dependent component takes a `Clock`. Tests simulate weeks in milliseconds.
- **Simulator:** `surplus simulate --weeks 4 --profile heavy-weekday` replays synthetic or recorded usage through collector → predictor → optimizer, with a **fake runner**, and reports utilisation and "user-blocked" incidents. CI fails if a known profile produces any incident. This is the most important test in the project.
- **No real quota in CI:** the runner has a `FakeClaude` that emits recorded stream-json.
- Structured JSON logging (`structlog`); every decision the optimizer makes is logged with its inputs.
- Migrations for every schema change; never edit applied migrations.
- Conventional commits; small PRs per phase step.
- `surplus doctor` checks: CLI installed and version, auth works, Podman rootless works, log dir readable, percent source fresh, channels reachable.

---

## 14. Delivery phases

Every phase has a **Definition of Done (DoD)**. Do not start phase N+1 before phase N's DoD is met.

### Phase 0 — Spikes (½–1 day)
Write findings to `docs/spikes/`:
1. Exact JSONL schema of the Claude Code logs on this machine (fields, dedup key, where usage sits).
2. Which authoritative percent sources exist (status-line JSON fields, stream-json rate-limit events, a `/usage` probe), and their exact format.
3. Headless auth inside a rootless Podman container with `setup-token`; confirm that the container runs do not share or corrupt the host's `~/.claude` state.
4. Does Claude Code's built-in sandbox work inside the container (nested bubblewrap with user namespaces)? If not → document the fallback (container-only isolation).
5. Telegram bot long-polling from behind NAT (expected: works).
6. **Reading reset times.** Find every way Claude reports the 5-hour and weekly `resets_at` without spending quota (status-line JSON, stream-json events, `/usage`), and document the format and update frequency of each in `docs/spikes/reset-times.md`. The Reset Watcher is built on the most reliable one.
**DoD:** every §2 fact confirmed or the plan amended via an ADR.

### Phase 1 — Skeleton and collector (2–3 days)
Repo scaffold, CI (`make check`), DB + migrations, JSONL ingestion, status-line spooler, manual calibration endpoint, `surplus doctor`, minimal dashboard (gauges + usage chart).
**DoD:** real 30-day history ingested idempotently; gauges match the CLI's `/usage` to within ±2 percentage points.

### Phase 2 — Projects and UI (2–3 days)
md parser/serialiser with round-trip tests, file watcher, Projects list/detail, editors, new-project wizard.
**DoD:** create a project in the UI → files on disk are correct; edit in an external editor → the UI updates within 2 s.

### Phase 3 — Runner and sandbox (3–4 days)
Container image, network isolation (LAN blocked, internet open), prompt composition, stream parsing, test verification, report schema + fallback, git branch + bundle snapshot, run detail page with a live log. Runs are **manual only** ("Run now" button).
**DoD:** a red-team test suite passes: the agent is instructed to write `/etc`, `~/`, to reach `192.168.x.x`, and to read `~/.ssh` → all fail; an internet fetch succeeds; a manual run on a toy project produces a branch, tests, and a valid report.

### Phase 4 — Predictor and optimizer (3–4 days)
Cost model fit, baseline quantile predictor, confidence, budget computation, window planner, dispatcher guard rails, kill switch, simulator in CI, plan timeline UI.
**DoD:** the simulator has 0 user-blocked incidents across 3 profiles × 4 weeks, with utilisation ≥ 75% on the "light user" profile; the kill switch pauses within 60 s of manual activity.

### Phase 5 — Autonomy and feedback loop (2–3 days)
Scheduled automatic runs, task selection, backlog updates, Inbox, decisions.md integration, circuit breaker, systemd install (`surplus install`).
**DoD:** 1 week of real unattended running with no user-blocked incident; every run has a report; answering a question in the Inbox influences the next run's prompt (visible in the run's prompt view).

### Phase 6 — Notifications and phone (2–3 days)
Channel interface, Email (SMTP out + IMAP replies), Telegram (pairing, inline buttons, commands), ntfy, daily digest, confirmation flow for merges.
**DoD:** from the phone: receive a run report, answer a question, pause for 3 h, approve a merge with the confirmation step. Unauthorised senders are ignored (tested).

### Phase 7 — Polish and v2 (ongoing)
GBM predictor (promoted only if it wins the backtest), calendar integration, Codex/Gemini CLI providers, insights page, data export, optional WhatsApp adapters, PWA install for the UI over Tailscale.

---

## 15. Risks and mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| The provider changes CLI output, log format or limits | Collector breaks | Versioned parsers, a "source stale" alert, `doctor`, contract tests on recorded fixtures |
| No reliable authoritative percent source | Budgets are guesses | Manual calibration + bigger reserves at low confidence; never exceed conservative defaults |
| Terms-of-service changes about automated or headless use | Account risk | Official CLI only, `docs/compliance.md`, easy global off switch |
| Agent produces low-quality code | Wasted quota | Small tasks, tests required, feedback score per project lowers its priority, circuit breaker |
| Prompt injection via the internet | Data leak or damage | Container + LAN block + no secrets mounted + git snapshots |
| Predictor is wrong during unusual weeks (deadlines) | User gets blocked | Real-time kill switch, P90 + reserves, one-tap "busy week" mode that doubles the reserves |

---

## 16. Open questions for the user (answer before Phase 5)
1. Default model for agent runs: Sonnet only, or allow Opus for "L"-size tasks when the surplus is large?
2. Projects root folder location?
3. Typical working hours and timezone, for the cold-start prior.
4. Primary phone channel: Telegram (recommended, free and two-way) or email only?
5. Should agents be allowed to install system packages inside their container (more capable, slower and larger images)?
