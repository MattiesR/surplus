# Spike 2 — Authoritative percent sources

Checked on: 2026-11-25 · Claude Code 2.1.332 · subscription type `pro`

Three sources exist. All of them report percent **and** reset time per bucket. Units differ, so normalise
everything to **percent 0–100** and **UTC datetimes** at the adapter boundary.

| Source | Costs quota? | Unit of utilisation | Unit of reset | When fresh |
|--------|--------------|---------------------|---------------|------------|
| A. `get_usage` control request (probe) | **No** (no inference; `total_cost_usd: 0`) | 0–100 | ISO-8601 string | On demand — live server call |
| B. `rate_limit_event` in `stream-json` | Only as part of a run | **0–1 fraction** | unix seconds | After every API response in a `-p` run |
| C. Status-line stdin JSON | No | 0–100 (`used_percentage`) | unix seconds | Whenever the user's interactive session re-renders the status line |
| D. Manual calibration | No | 0–100 | entered | When the user types it |

## A. `get_usage` probe (best source → primary for the Reset Watcher)
`claude -p --input-format stream-json --output-format stream-json --verbose --no-session-persistence`,
then write two control requests to stdin and close it **without ever sending a user message**:
```json
{"type":"control_request","request_id":"r1","request":{"subtype":"initialize"}}
{"type":"control_request","request_id":"r2","request":{"subtype":"get_usage","skip_behaviors":true}}
```
Response (trimmed; full copy in `backend/tests/fixtures/probe/get_usage_response.json`):
```json
{"type":"control_response","response":{"subtype":"success","request_id":"r2","response":{
  "subscription_type":"pro","rate_limits_available":true,
  "rate_limits":{
    "five_hour":{"utilization":6,"resets_at":"2026-11-26T00:30:00.111920+00:00"},
    "seven_day":{"utilization":1,"resets_at":"2026-12-02T19:00:00.111941+00:00"},
    "seven_day_opus":null,"seven_day_sonnet":null, …},
  "limits":[{"kind":"session","percent":6,"resets_at":"…","severity":"normal"},
            {"kind":"weekly_all","percent":1,"resets_at":"…"}],
  "seven_day_breakdown":{"rows":[{"key":"claude_code","percent_of_limit":1},
                                  {"key":"chat","percent_of_limit":0}, …]},
  "model_scoped":[] }}}
```
- Takes ~3–4 s (CLI start-up). Session cost reported as 0, no session file written.
- The SDK method is named `usage_EXPERIMENTAL_MAY_CHANGE_DO_NOT_RELY_ON_THIS_API_YET` → **contract-test it
  against the recorded fixture**, keep the parser tolerant (unknown keys ignored, every bucket nullable), and
  raise a "source stale / format changed" alert if parsing fails.
- **Bonus:** `seven_day_breakdown` splits weekly usage by surface (`claude_code`, `chat`, `cowork`, `other`).
  That directly measures "invisible" chat usage, so §5.2 can use it instead of only inferring it from Δ%.
- Buckets map to `LimitBucket` ids: `five_hour`, `seven_day` (= plan's `weekly_all`), `seven_day_opus`,
  `seven_day_sonnet`, … and `model_scoped[]` entries (`weekly_model:<display_name>`). For this Pro account
  only `five_hour` and `seven_day` are non-null.

## B. `rate_limit_event` (stream-json, during agent runs)
Captured from a 1-turn Haiku run (fixture: `backend/tests/fixtures/probe/run_stream.jsonl`):
```json
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1795653000,
  "rateLimitType":"five_hour","isUsingOverage":false,
  "unifiedWindows":{"five_hour":{"utilization":0.06,"resetsAt":1795653000,"status":"allowed"},
                    "seven_day":{"utilization":0.01,"resetsAt":1796238000,"status":"allowed"}}},
 "uuid":"…","session_id":"…"}
```
`status` ∈ `allowed | allowed_warning | rejected`. The dispatcher's live hard stop (§7.3) watches
`unifiedWindows.*.utilization` and `status`.

The same run's `assistant` events carry `message.usage` exactly like the JSONL logs, and the `result` event
carries total `usage`, `modelUsage` and `total_cost_usd` (API-price equivalent, useful as a cost-model feature).

## C. Status-line JSON
The CLI builds `rate_limits: { five_hour: {used_percentage, resets_at}, seven_day: {used_percentage, resets_at} }`
(resets_at = unix seconds) and pipes the whole status object to the configured `statusLine.command` on stdin.
Passive and free, but only fresh while the user has an interactive session open. Installing it means editing
the user's `~/.claude/settings.json` → **opt-in** via `surplus install --statusline`, never silent.
Not live-verified yet (it needs the user's interactive session); the schema comes from the shipped CLI.

## Conclusion
Reset Watcher order: **A (every 15 min + around runs) → B (during runs) → C (passive) → D (manual)**.
