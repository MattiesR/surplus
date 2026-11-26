# Surplus

Put the unused part of your Claude subscription to work.

A Claude subscription has a 5-hour limit and a weekly limit, and most weeks a good share of both goes unused.
Surplus watches your usage, predicts how much you will still need before the next reset, and lets Claude Code
agents work through your project backlogs with what is left. You stay first: agents stop when you start
working, and every change waits for your review.

## What it does

- **Tracks usage.** It reads the percentages and reset times straight from Claude and adds the Claude Code
  logs on this machine. It splits your weekly use into you, chat and other apps, and agents.
- **Plans.** It learns your weekly rhythm and forecasts your own use until the weekly reset. It plans agent
  work only in the room that is left, and saves most of that for the end of the week.
- **Runs agents safely.**
  - Each run works on one task from a project's `backlog.md`.
  - Runs happen in a rootless Podman container with no access to your LAN, on a branch of their own, with a
    hard stop in % of the limit.
  - Agents never push or merge: you review the diff and merge or discard.
- **Asks you.** Agents can ask questions. You answer them in the Inbox, and the answers go to the project's
  `decisions.md`.
- **Gives you control.**
  - Automatic runs are off until you run `surplus auto on`.
  - `surplus pause 4h` and the Pause button stop all runs.
  - A run stops on its own when you become active.

## Requirements

- Linux with systemd, Python 3.12 and [uv](https://docs.astral.sh/uv/), and Node 22 (to build the dashboard).
- Rootless Podman, for agent runs.
- Claude Code (`claude`), signed in to a Pro or Max subscription.

## Quick start

```bash
make install            # backend and frontend dependencies
make build              # build the dashboard
make runner-image       # the agent container image
uv run surplus doctor   # check the CLI, the limits probe, Podman and the logs
uv run surplus install  # run in the background at login, and add "Surplus" to the app menu
uv run surplus app      # open the dashboard in its own window
```

Useful commands:

| Command | What it does |
|---|---|
| `surplus status` | Current usage and reset times |
| `surplus hours "mon-fri 09:00-18:00"` | Your usual working hours, a starting point until Surplus has history |
| `surplus auto on\|off` | Turn automatic agent runs on or off |
| `surplus pause 4h` / `surplus resume` | Pause and resume all agent runs |
| `surplus link` | Print a sign-in link for a normal browser tab |
| `surplus uninstall` | Remove the service and the launcher; your data stays |

The dashboard listens on `127.0.0.1:8765` only.

## Projects

A project is a folder with a `.surplus/` directory of plain Markdown files: `project.md` (settings),
`goals.md`, `backlog.md` (tasks such as `T-001`), and `decisions.md`. You edit them in the dashboard or in
any editor.

## Development

```bash
make check   # ruff, mypy --strict, tsc --strict, pytest, vitest
make e2e     # Playwright against an isolated backend
make dev     # backend on :8765, Vite on :5173
```

Tests never use real quota: Claude is faked.

## Docs

- `IMPLEMENTATION_PLAN.md`: the build spec.
- `docs/decisions/`: architecture decisions (ADRs).
- `docs/changelog.md`: what changed, per phase.
- `docs/compliance.md`: how subscription tokens are handled.
- `CLAUDE.md`: working notes for Claude Code.
