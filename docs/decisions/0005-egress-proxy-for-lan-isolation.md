# 0005 — Internal network + filtering egress proxy for agent containers

- Status: accepted · Date: 2026-11-25

## Context
Plan §9.2 requires outbound internet but no access to the LAN, the host or the Surplus API. Spike 3 found that
rootless Podman's default network (pasta) reaches the LAN (the router answered from inside a container), and a rootless
setup cannot add host nftables rules. A Podman `--internal` network blocks everything, internet included.

## Decision
- Each agent container is attached **only** to an internal network (`surplus-agents`).
- One egress-proxy container (Surplus-owned, no secrets, read-only) sits on that internal network and on the default
  network. It accepts HTTP `CONNECT host:443` and plain HTTP, resolves the name itself, and refuses any destination in
  RFC1918, 100.64/10, 169.254/16, 127/8, ::1, fc00::/7, fe80::/10, multicast, or the host's own addresses. Every refusal is logged.
- The agent container gets `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY=` env vars. Tools that ignore proxies simply have no route out.
- The red-team suite (Phase 3 DoD) checks both a direct connection and a proxied connection to `192.168.x.x`.

## Consequences
Small extra moving part (the proxy), but the policy is enforced in code we test, and raw-socket escapes are impossible
because the internal network has no route out. DNS rebinding is handled by checking the resolved address, not the name.
