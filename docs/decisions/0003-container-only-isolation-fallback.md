# 0003 — Container-only isolation when nested bubblewrap is unavailable (provisional)

- Status: **accepted** · Date: 2026-11-25 (confirmed by spike 4: nested `bwrap` fails with EPERM in rootless Podman)

## Context
Ubuntu 24.04 sets `kernel.apparmor_restrict_unprivileged_userns=1`; `bwrap --unshare-user` fails on the host. Nested
bubblewrap inside rootless Podman is likely to fail too, unless the container is given broader privileges, which would
weaken the outer layer.

## Decision
Do **not** loosen the container's seccomp/AppArmor confinement to make the inner sandbox work. Run Claude Code inside the
container without its own sandbox; the rootless container (only `/workspace` mounted rw, read-only root, LAN blocked,
no secrets) is the isolation boundary. The red-team suite (Phase 3 DoD) must pass with this setup.

## Consequences
One isolation layer instead of two, but the stronger one is kept intact. `--dangerously-skip-permissions` stays restricted to
the container.
