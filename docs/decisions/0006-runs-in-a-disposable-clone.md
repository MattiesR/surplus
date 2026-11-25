# 0006 — Agent runs work in a disposable clone; host CLI binary is mounted read-only

- Status: accepted · Date: 2026-11-25

## Context
Plan §8.1 says "git worktree or a branch in the project repo; refuse if the working tree is dirty", and §9.2 mounts
"the project folder at /workspace (rw) only". A worktree does not work inside a container (its `.git` file points
at the main repo's `.git/worktrees/…`, which is not mounted). Mounting the project itself gives the agent write
access to the user's real `.git`: an agent could delete history, plant hooks or set `core.fsmonitor`, and the next
host-side `git status` (by Surplus *or the user*) would execute that code on the host.

Plan §9.2 also bakes the Claude CLI into the image. The CLI is a ~400 MB native binary that updates itself often.

## Decision
- Each run gets `git clone --no-local --no-hardlinks` of the project into `~/.local/share/surplus/runs/rN/repo`
  (objects copied, no shared files, origin removed), on a new branch `surplus/T-xxx-slug` (suffix `-2`, `-3` if
  the name is taken). Only that clone is mounted at `/workspace`.
- Trust boundary: after the agent ran, the clone is untrusted. Every git command that reads it (status, add, commit
  of leftovers) runs **inside the container**. The branch is brought back with `git fetch` from the clone; git
  ignores repository-level `uploadpack.packObjectsHook`, so fetching from an untrusted local repo is safe.
- A `git bundle --all` snapshot of the project is still taken before every run (plan §9.2).
- The user's checkout is never touched by a run. A dirty working tree is therefore not a reason to refuse: the
  run starts from `HEAD` and the UI warns that uncommitted changes are not included.
- Merge (from the UI only) runs on the host in the user's own repo: it refuses a dirty tree or a different
  checked-out branch, merges `--no-ff`, and aborts cleanly on conflict. Discard deletes the branch.
- For repositories that Surplus creates, `.surplus/` goes into the repo-local `.git/info/exclude` and an empty
  first commit is made (so a run has a base). Existing repositories are never changed.
- The host's CLI binary (symlinks resolved) is bind-mounted read-only at `/usr/local/bin/claude`. The container
  runs exactly the host's CLI version; the image stays at ~730 MB without it.

## Consequences
- `rm -rf .git`, hooks, config tricks and history rewrites in the agent's workspace cannot affect the project
  (tested: `test_agent_destroying_its_git_dir_cannot_hurt_the_project`, red-team `test_git_bundle_restores…`).
- Disk: one full copy of the repository per run, deleted when the run is merged or discarded.
- The agent cannot see uncommitted user work. This is intended.
