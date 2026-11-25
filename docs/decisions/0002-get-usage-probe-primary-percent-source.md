# 0002 — `get_usage` control request is the primary percent/reset source

- Status: accepted · Date: 2026-11-25

## Context
Plan §5.1 ranks the status-line JSON first and a "/usage probe" third ("if it costs no quota"). Spike 2 found that
`claude -p --input-format stream-json` answers a `get_usage` control request with every bucket's utilisation and
`resets_at`, without any inference (`total_cost_usd: 0`). The status line is only fresh while the user works interactively,
and installing it changes the user's own Claude settings.

## Decision
1. The Reset Watcher polls the `get_usage` probe every 15 min, right before and after each run, and a few minutes after every expected reset.
2. Stream-json `rate_limit_event`s from agent runs are stored as readings too (source `stream`).
3. The status-line spooler is **opt-in** (`surplus install --statusline`), because it edits `~/.claude/settings.json`.
4. Manual calibration stays as the last fallback.

## Consequences / risks
The SDK method is explicitly marked experimental. Mitigation: a recorded-fixture contract test, a tolerant parser, and a
"format changed" alert. When the probe breaks, the watcher degrades to the other sources and the confidence drops (§6.4),
and after 1 h without a fresh reset time, runs pause (§5.1).
