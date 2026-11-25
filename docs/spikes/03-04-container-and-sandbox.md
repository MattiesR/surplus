# Spikes 3 & 4 — Headless auth in a container, nested sandbox, network isolation

First pass 2026-11-25 (Podman missing). Re-run 2026-11-25 after the user installed Podman 4.9.3 and ran
`claude setup-token`. **Result: Phase 3 entry gate passed.**

## Results

| Check | Result |
|-------|--------|
| Rootless Podman | ✅ `rootless=true`, overlay storage in `~/.local/share/containers`; container root = host uid 1000 |
| Claude CLI inside a container | ✅ host binary bind-mounted **read-only** into `debian:bookworm-slim` runs (`2.1.332`) under `--read-only --cap-drop=all --security-opt no-new-privileges --network none` |
| Host `~/.claude` untouched | ✅ no top-level change; with `CLAUDE_CONFIG_DIR=/cfg` the CLI writes only into the per-run dir |
| No credential leak | ✅ without a token the CLI in the container reports `rate_limits_available: false`, `subscription_type: null` |
| Auth with `CLAUDE_CODE_OAUTH_TOKEN` | ✅ `claude auth status` → `loggedIn: true, authMethod: oauth_token`; one Haiku call (397 in / 4 out tokens) succeeded |
| `get_usage` with the setup token | ⚠ `subscription_type: null`, `rate_limits_available: false`: the long-lived token cannot read limits. The host probe (normal login) stays the primary source |
| Limits during a container run | ✅ `rate_limit_event` carries `unifiedWindows.five_hour` + `seven_day` (same resets as the host probe) |
| Usage logging | ✅ session JSONL written to `$CLAUDE_CONFIG_DIR/projects/-work/…`; the collector parses it (fixtures in `backend/tests/fixtures/runner/`) |
| Token leakage | ✅ no `sk-ant` string in the per-run config dir, the stream or the session log |
| Nested bubblewrap (spike 4) | ❌ `bwrap: Creating new namespace failed: Operation not permitted` under default confinement → ADR 0003 accepted: container-only isolation |
| Default rootless network (pasta) | ⚠ internet ✅, but **LAN reachable** (router `192.168.0.1:53` answered from inside) |
| `--internal` Podman network | ✅ LAN blocked, ✅ internet blocked |

## Consequences for Phase 3
1. **Network:** the default network fails the "LAN blocked" requirement, and rootless Podman cannot install nftables
   rules on the host. Design (ADR 0005): agent containers join an **internal** network only; a small **egress proxy**
   container is attached to the internal network and to the default network, and allows only HTTP(S) `CONNECT` to
   public addresses (denies RFC1918, CGNAT, link-local, loopback, multicast and the host, checked *after* DNS
   resolution). The agent gets `HTTPS_PROXY`/`HTTP_PROXY`. Raw sockets cannot leave the internal network at all.
2. **Image size / disk:** the Claude CLI is a 400 MB native binary. Mounting the host binary read-only (as tested)
   avoids copying it into every image. Disk was freed by the user (17 GB available on 2026-11-25).
   Overlay storage does **not** work on the NTFS mount, so images stay in `~/.local/share/containers`.
4. **Per-run config dir:** each run gets its own `CLAUDE_CONFIG_DIR`; the collector must ingest its `projects/`
   folder (agent usage), and the host `~/.claude` is never mounted.
3. **Sandbox:** Claude Code's own sandbox stays off inside the container (it cannot create namespaces);
   `--dangerously-skip-permissions` is used only inside the container, never on the host.

## Spike 5 — Telegram long-polling
`https://api.telegram.org/bot/getMe` → HTTP 404 in 0.25 s from behind NAT, so the API is reachable outbound.
`getUpdates` long-polling needs no inbound port. A full check needs a bot token from @BotFather (Phase 6).
