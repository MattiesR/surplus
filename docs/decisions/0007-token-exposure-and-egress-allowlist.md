# 0007 — Residual risk: the setup token is readable inside the run container; optional egress allowlist

- Status: accepted (decision on the default left to the user) · Date: 2026-11-25

## Context
The CLI in the container needs `CLAUDE_CODE_OAUTH_TOKEN`. Surplus passes it only to the `claude` exec, as
`podman exec -e NAME` with the value in the child environment (never on a command line, never in the container
config, never to the test command). Measured on 2026-11-25:
- Claude Code removes the token from its Bash tool's environment (`env | grep -c CLAUDE_CODE_OAUTH_TOKEN` → 0,
  asked of a real Haiku agent in `test_agent_redteam.py`).
- But a process running as the same uid can read another process's `/proc/<pid>/environ` in the container
  (verified with a dummy variable). A prompt-injected agent could therefore read the token from the CLI process
  and send it to any public host, because plan §9.2 requires open outbound internet.
The token is scoped to inference only (it cannot even read limits), has a long lifetime, and can be revoked by the
user (claude.ai → settings, or by running `claude setup-token` again).

## Decision
- Keep the plan's default: open internet through the filtering proxy (LAN, host and metadata addresses refused).
- Add an **optional host allowlist** to the egress proxy: `SURPLUS_EGRESS_ALLOW="api.anthropic.com,pypi.org,…"`
  (`*.example.com` for subdomains). With it set, the agent can reach only those hosts, which removes the
  exfiltration path to arbitrary servers. The proxy is restarted automatically when the list changes.
- Document the residual risk in `docs/compliance.md`, and ask the user whether the allowlist should become the
  default (plan §16-style open question).

## Consequences
With the default, a successful prompt injection could leak the token; the user would need to revoke it. With the
allowlist, package installs from hosts not on the list fail until the user adds them.
