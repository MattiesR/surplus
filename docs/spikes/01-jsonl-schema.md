# Spike 1 — Claude Code JSONL log schema

Checked on: 2026-11-25 · Claude Code 2.1.332 · Ubuntu 24.04 · logs at `~/.claude/projects/`

## Layout
- `~/.claude/projects/<cwd-with-slashes-as-dashes>/<session-uuid>.jsonl` — one file per session, flat (depth 1).
- On this machine: 32 project dirs, 78 files, 482 MB, history from 2026-10-14.
- A full scan (`json.loads` per line) of all 482 MB takes ~3 s, which is within the < 10 s target for 30 days.

## Line types
Each line is one JSON object with a `type`. Types seen: `assistant`, `user`, `system`, `attachment`,
`api-request`, `api-request-blob`, `data-flow-records`, `last-prompt`, `mode`, `permission-mode`,
`session-owner`, `ai-title`, `file-history-snapshot`, `file-history-delta`, `tools-used`, `cost-state`,
`derived-from`, … Only `assistant` lines carry usage. **Unknown types must be ignored, not rejected.**

## Assistant line (relevant fields)
```jsonc
{
  "type": "assistant",
  "timestamp": "2026-11-25T20:03:04.322Z",   // ISO-8601 UTC
  "sessionId": "404aac37-…",
  "requestId": "req_011Ch…",
  "isSidechain": false,                      // true for subagent turns
  "entrypoint": "cli" | "claude-desktop",    // desktop-app Claude Code sessions also land here
  "cwd": "/mnt/…/vtx-autopilot",
  "message": {
    "id": "msg_011Ch…",
    "model": "claude-opus-5-5",              // also "<synthetic>" for locally generated messages → skip
    "usage": {
      "input_tokens": 2,
      "output_tokens": 263,
      "cache_creation_input_tokens": 14590,
      "cache_read_input_tokens": 25802,
      "cache_creation": {"ephemeral_1h_input_tokens": 14590, "ephemeral_5m_input_tokens": 0},
      "output_tokens_details": {"thinking_tokens": 21},
      "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
      "service_tier": "standard", "speed": "standard", "iterations": [ … ]
    }
  }
}
```

## Dedup key — differs from the plan
- One API response is written as **several lines** (one per content block): 11,526 assistant lines map to
  4,631 unique `message.id`s. Every copy has an **identical** `usage` object (0 conflicts found).
- **145 message ids appear in more than one session**, and 205 span more than one file: resumed and forked
  sessions copy earlier history into the new file (`derived-from` lines).
- ⇒ The plan's key `(session_id, message_id)` would **double count** copied history. Use the global
  `message.id` as the dedup key (fall back to `requestId` when `id` is missing). See ADR 0001.

## Models seen
`claude-opus-5-5` (97%), `claude-opus-5`, `claude-fable-5-1`, `claude-fable-5-2`, `<synthetic>`.

## Parser rules derived from this spike
1. Read line by line, starting at a byte offset; ignore a last line without a trailing `\n`, because it may be a partial write.
2. Skip non-`assistant` lines, lines without `message.usage`, and `model == "<synthetic>"`.
3. Key = `message.id`; `INSERT … ON CONFLICT DO NOTHING`.
4. Cursor = (path, inode, size, offset). If the inode changes or the file shrinks, re-read from 0; dedup makes that safe.
