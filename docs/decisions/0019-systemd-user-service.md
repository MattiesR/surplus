# 0019 — Running as a systemd user service

- Status: accepted · Date: 2026-11-26

## Context
Phase 5 asks for `surplus install`, so Surplus runs unattended without a terminal window.

## Decision
1. **A user service, not a system one.** `~/.config/systemd/user/surplus.service` runs `<venv>/bin/surplus
   serve --host 127.0.0.1` as the user, the same user that owns the rootless Podman setup and the CLI login.
   No root is needed.
2. **No secrets in the unit.** It carries PATH, a fixed list of non-secret `SURPLUS_*` settings (never
   anything named like a token, secret, password or key), `SURPLUS_SERVICE=1` and `UMask=0077`. The tokens
   stay in `~/.config/surplus/` and are read by the server.
3. **No token in the journal.** Under the service (`SURPLUS_SERVICE`), `serve` does not print the sign-in
   link. `surplus link` prints it on demand.
4. **No `NoNewPrivileges=`.** Rootless Podman needs the setuid `newuidmap`/`newgidmap` helpers. Isolation of
   agents is the container's job (ADR 0003, 0005), not the daemon unit's.
5. **Starts at login.** Starting at boot without a login needs `loginctl enable-linger`, which is the user's
   choice; `surplus doctor` reports which applies. `Restart=on-failure` every 10 s also covers a project
   drive that mounts late.
6. `surplus install` refuses to start when something already listens on the port (a manual `surplus serve`),
   `--dry-run` prints the unit, and `surplus uninstall` removes it and keeps all data.
