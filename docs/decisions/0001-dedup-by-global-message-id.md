# 0001 — Deduplicate usage events by global `message.id`

- Status: accepted · Date: 2026-11-25

## Context
Plan §4.2 dedups `usage_event` by `(session_id, message_id)`. Spike 1 found that resumed/forked sessions copy
history into new session files: 145 message ids occur in more than one session. Every copy has identical usage.

## Decision
The dedup key is `message.id` alone (globally unique per API response), with `requestId` as fallback.
`usage_event.session_id` stores the session of the **first** file it was seen in.

## Consequences
No double counting of copied history. If two different responses ever shared an id (not observed), we would under-count,
which errs on the safe side (less estimated spend → the cost model leans on percent readings, which are authoritative).
