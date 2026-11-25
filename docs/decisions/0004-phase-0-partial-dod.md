# 0004 — Start Phase 1 while spikes 3–4 wait on user action

- Status: accepted · Date: 2026-11-25

## Context
Phase 0 DoD requires all six spikes. Spikes 3–4 need Podman (not installed; needs `sudo`) and `claude setup-token`
(interactive). Phases 1–2 (collector, projects, UI) do not depend on those results; Phase 3 (runner) does.

## Decision
Proceed with Phases 1–2. Spikes 3–4 are a **hard entry gate for Phase 3**: no runner code that executes agents is
written before they are verified. This is the safer reading: nothing spends quota or runs agents in the meantime.
