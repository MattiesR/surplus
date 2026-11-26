# Surplus — working notes for Claude Code

The build spec is `IMPLEMENTATION_PLAN.md`. Build phase by phase; a phase is done when its DoD is met,
`make check` is green, and `docs/changelog.md` has an entry. Ambiguity → choose the safer option (less quota,
fewer permissions, more user control) and record an ADR in `docs/decisions/NNNN-title.md`.

## Status
- Phase 0: all spikes done (`docs/spikes/`). Phase 3 entry gate passed 2026-11-25: setup-token auth works in a
  rootless Podman container; isolation is container-only (ADR 0003); LAN blocked via internal network + egress proxy (ADR 0005).
- Phase 1 (skeleton + collector + minimal dashboard): done.
- Phase 2 (projects, md parsers, watcher, UI): done.
- Phase 3 (runner + sandbox, manual runs only): done.
- Phase 4 (predictor + optimizer): done. Cost model (ADR 0009), baseline (ADR 0010/0011), budget/planner
  (ADR 0012), simulator (ADR 0013, `surplus simulate`, runs in `make check`), guard rails (ADR 0014: kill
  switch, `surplus pause`, circuit breaker), live plan + timeline (`/api/plan`). Runs stay manual until Phase 5;
  the dispatcher already enforces pause and the kill switch.
- Phase 5 built: task selection (ADR 0016), automatic runs (ADR 0017, opt-in via `surplus auto on`, off by
  default), the Inbox (ADR 0018; answers live in decisions.md) and `surplus install` (ADR 0019, systemd user
  service). DoD still needs a week of real unattended running, which the user starts. Next: Phase 6 (Telegram).
- Plan §16 answered (ADR 0015): project model for auto runs, working hours mon-fri 09-18 (`surplus hours`),
  Telegram for Phase 6, no apt in agent containers.
- Surplus is registered as its own Surplus project (`.surplus/` is excluded locally, never committed). Agent runs
  on it get Opus/high effort and may change any code, but never a weaker sandbox (ADR 0008). Review merges of
  `backend/surplus/runner/` with care: merged code runs on the host.

## Commands
- `make install` · `make check` (ruff, ruff format, mypy --strict, tsc --strict, pytest, vitest) · `make e2e`
- `make runner-image` · `make redteam` (real Podman, no quota) · `make redteam-agent` (real Haiku agent: spends quota;
  only run it deliberately). Tests marked `podman`/`quota` are excluded from `make check`.
- `make build` regenerates `openapi.json` + `frontend/src/api/schema.d.ts` and builds the SPA into `backend/surplus/static`.
- `uv run surplus serve|ingest|probe|status|doctor|statusline|simulate|pause|resume|hours|auto|install|uninstall|link|app …`
  (`surplus app` = the dashboard in its own window, ADR 0020).
- `uv` lives in `~/.local/bin` (the Makefile adds it to PATH).

## Engineering standards (plan §13)
- **Typed everywhere:** `mypy --strict`, `tsc --strict`; Pydantic models at every boundary.
- **Pure core, thin shell:** predictor, budget, planner, reset-watcher scheduling, split and the state machine are pure
  functions with no I/O. I/O lives in adapters (`collector/sources/`, `collector/store.py`, `api/`).
- **Fake time:** every time-dependent component takes a `Clock` (`surplus.clock`); tests use `FakeClock`.
- **No real quota in tests or CI:** use `backend/tests/fixtures/fake_claude.py` (FakeClaude) and recorded fixtures.
- **Internal currency is % of a limit (0–100), never tokens.** Normalise units only in `collector/limits.py`.
- **Reset times are only ever read from Claude** — never computed from scratch, hard-coded, or typed in by the user.
  Projections are `last reported reset + k × period` (`reset_watcher.project_reset`).
- Structured JSON logging (`structlog`); log every decision with its inputs.
- Every schema change gets a new Alembic migration in `backend/surplus/db/alembic/versions/`; never edit an applied one.
  Migrations use plain `sa.*` types, not model types.
- Conventional commits; small commits per phase step.
- Never read, copy, log or send subscription tokens; only the official CLI uses them (plan §9.4, `docs/compliance.md`).

## Facts learned on this machine (see `docs/spikes/`)
- Log dedup key is the global `message.id` (ADR 0001); one response = several identical-usage lines.
- Primary percent/reset source: the zero-quota `get_usage` control request (ADR 0002). It is marked experimental:
  keep `test_limits.py` contract tests green against recorded fixtures.
- `rate_limit_event` utilisation is a 0–1 fraction; probe and status line use 0–100.
- The repo is on an NTFS (`fuseblk`) mount: `chmod` is a no-op there and `sed -i` prints a harmless
  "preserving permissions" warning. Secrets and the API token live in `~/.config/surplus/` (ext4, 0600).
- Ubuntu 24.04 blocks unprivileged user namespaces for `bwrap` (AppArmor) → see ADR 0003.
- Never `pkill -f`/`pgrep -f` a pattern that also appears in your own command line: it kills the shell running it.
  Stop background servers by PID file.
- In containers the setup token cannot read limits via `get_usage`; use the stream's `rate_limit_event`.
  The host probe stays primary. Only `CLAUDE_CODE_OAUTH_TOKEN` is passed (as `podman run -e NAME`, value from
  the child process env, never on a command line); other secrets in `secrets.env` never reach a container.
- Runner trust boundary (ADR 0006): never run host git inside a run clone after the agent touched it; use the
  container. Run containers carry an instance label; only touch containers of your own instance.
- Rootless Podman: `--tmpfs /home/agent:U` gives the keep-id user a writable home; `podman logs --since` needs
  RFC 3339 with a colon in the offset.
- TypeScript is pinned to 5.x because `openapi-typescript` needs the JS compiler API (TS 7 has none).
