# Compliance notes (re-check against each provider's current terms)

Last reviewed by the user: _not yet_ · Written: 2026-11-25

## What Surplus does with your Claude subscription
- It only drives the **official Claude Code CLI** (`claude`). It never calls model APIs itself and never proxies,
  shares or resells access (plan §1.2).
- It **never reads, copies, logs or transmits** subscription credentials. The CLI reads its own login. For agent
  containers (Phase 3) the user creates a token with the official `claude setup-token`, and Surplus passes it only
  as an environment variable to the CLI inside the container.
- Agent runs (Phase 3) execute the official CLI inside a rootless Podman container. The token from
  `claude setup-token` lives in `~/.config/surplus/secrets.env` (0600); Surplus reads it only to hand it to that
  CLI process (`podman exec -e CLAUDE_CODE_OAUTH_TOKEN`, value via the environment, never on a command line or in
  the container config, never to test commands, never logged).
  **Residual risk (ADR 0007):** code running inside the container can read the CLI process's environment, so a
  prompt-injected agent could leak the token over the open internet. Mitigations: LAN/host are blocked; set
  `SURPLUS_EGRESS_ALLOW` to restrict agents to known hosts; revoke the token if a run looks suspicious.
- Limit readings use the CLI's `get_usage` control request, which makes no model call (`total_cost_usd: 0`).
  It is an SDK method marked experimental; if it changes or disappears, Surplus falls back to other sources.
- Data stays local (`~/.local/share/surplus`, `~/.config/surplus`). Nothing is sent to third parties except
  notification channels the user configures (Phase 6).

## To re-check periodically
- [ ] Anthropic consumer terms / usage policy on automated or headless use of a subscription with Claude Code.
- [ ] Whether `claude setup-token` is still the recommended way to authenticate headless runs.
- [ ] That running the CLI with `--permission-mode bypassPermissions` inside a container is acceptable use.
- [ ] Any rules on background or scheduled agent usage and on rate-limit behaviour.
- [ ] Notification providers' terms (Telegram bot API, email provider, WhatsApp pricing) before Phase 6.

Global off switch: stop the service (`systemctl --user stop surplus`, Phase 5) or `surplus pause` (Phase 4).
