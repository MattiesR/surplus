# Spike 6 — Reading reset times

Checked on: 2026-11-25 ~21:04 local (Europe/Brussels, UTC+1).

| Bucket | Reported `resets_at` (UTC) | Local | Matches plan observation |
|--------|----------------------------|-------|--------------------------|
| `five_hour` | 2026-11-26T00:30:00Z | Thu 01:30 | ✅ "5-hour limit, resets Thu 01:30" |
| `seven_day` | 2026-12-02T19:00:00Z | Wed 20:00 | ✅ "Weekly · all models, resets Wed 20:00" |

## Formats per source
- **`get_usage` probe:** `rate_limits.<bucket>.resets_at` = ISO-8601 with micro-seconds and offset
  (`2026-11-26T00:30:00.111920+00:00`). Round to the nearest second, because the micro-seconds are server noise.
  Also present in `limits[].resets_at`. `seven_day_breakdown.window_started_at` gives the weekly window **start**
  (`2026-11-25T19:00:00Z`, i.e. last Wednesday's reset → a 7-day period, confirms the weekly grid).
- **`rate_limit_event`:** `unifiedWindows.<bucket>.resetsAt` = unix seconds (int).
- **Status line:** `rate_limits.<bucket>.resets_at` = unix seconds.

## Update frequency
- Probe: live on every call; ~3–4 s per call; no quota. 15-min polling = 96 CLI start-ups/day, which is acceptable.
- Stream events: every API response during runs.
- Status line: on each re-render of the user's interactive session.

## Observations relevant to the planner
- Reset times sit on whole hours/half hours, which suggests a server-side grid. The planner still treats
  them only as reported values (plan §2.3) and never computes them.
- Δ between the next 5-h reset and the weekly reset is not a multiple of 5 h (00:30 → 19:00 next Wed), so
  5-h projections must be anchored on the last reported `five_hour.resets_at`, never on the weekly reset.

## Open
- What `five_hour.resets_at` reports when the window has seen no use (null vs. a projected grid time).
  To be observed by the Reset Watcher in Phase 1; the parser accepts `null`.
