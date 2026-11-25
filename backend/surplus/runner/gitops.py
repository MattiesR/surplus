"""Host-side git operations for runs (ADR 0006).

Trust boundary: these functions run git on the host **only** in the user's project repository (trusted
config) or in a run clone *before* the agent has touched it. Once the agent has run, its clone is
untrusted: a planted `core.fsmonitor`, hook or alias could execute code, so every command that reads the
agent's clone runs inside the run container (see `service.py`). Bringing the agent's branch back uses
`git fetch` from the clone, which runs `upload-pack` there; git ignores repository-level
`uploadpack.packObjectsHook` precisely so that fetching from an untrusted local repo is safe.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT = 120
# Fixed identity and no prompts for commands Surplus runs itself.
_BASE = [
    "git",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "credential.helper=",
    "-c",
    "protocol.file.allow=always",
]


class GitError(RuntimeError):
    pass


def _env() -> dict[str, str]:
    """A minimal environment: the user's global git config applies (it is trusted), nothing else leaks in."""
    env = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(Path.home())}
    if "GIT_CONFIG_GLOBAL" in os.environ:
        env["GIT_CONFIG_GLOBAL"] = os.environ["GIT_CONFIG_GLOBAL"]
    return env


def git(repo: Path, *args: str, timeout: float = GIT_TIMEOUT, check: bool = True) -> str:
    proc = subprocess.run(
        [*_BASE, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env(),
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])}: {(proc.stderr or proc.stdout).strip()[-400:]}")
    return proc.stdout


@dataclass(frozen=True)
class RepoState:
    head_sha: str | None  # None = no commits yet
    branch: str | None  # None = detached HEAD
    dirty_files: list[str]


def repo_state(repo: Path) -> RepoState:
    """HEAD, current branch, and tracked files with uncommitted changes (`.surplus/` is ignored)."""
    head = git(repo, "rev-parse", "--verify", "--quiet", "HEAD", check=False).strip() or None
    branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip() or None
    status = git(repo, "status", "--porcelain=v1", "--untracked-files=no", "-z") if head else ""
    dirty = [e[3:] for e in status.split("\0") if len(e) > 3]
    dirty = [p for p in dirty if not p.startswith(".surplus/")]
    return RepoState(head, branch, dirty)


def branch_exists(repo: Path, branch: str) -> bool:
    return bool(git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).strip())


def unique_branch(repo: Path, base_name: str) -> str:
    name = base_name
    n = 2
    while branch_exists(repo, name):
        name = f"{base_name}-{n}"
        n += 1
    return name


def snapshot_bundle(repo: Path, dest: Path) -> None:
    """`git bundle` of every ref: lets the user restore the repo even after the worst case (plan §9.2)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "bundle", "create", str(dest), "--all", timeout=600)


def prepare_clone(repo: Path, dest: Path, branch: str) -> str:
    """Clone `repo` into `dest` (copied objects, no hardlinks, no remote) on a new `branch`.

    Returns the base commit sha.
    """
    if dest.exists():
        raise GitError(f"{dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [*_BASE, "clone", "--quiet", "--no-local", "--no-hardlinks", str(repo), str(dest)],
        capture_output=True,
        text=True,
        timeout=600,
        env=_env(),
    )
    if proc.returncode != 0:
        raise GitError(f"git clone: {proc.stderr.strip()[-400:]}")
    git(dest, "remote", "remove", "origin")
    git(dest, "checkout", "--quiet", "-b", branch)
    return git(dest, "rev-parse", "HEAD").strip()


def fetch_branch(repo: Path, clone: Path, branch: str) -> str | None:
    """Copy `branch` from the run clone into the project repo. Returns its sha, or None if it is missing.

    Never overwrites an existing branch in the project (the name was made unique before the run).
    """
    if branch_exists(repo, branch):
        raise GitError(f"branch {branch} already exists in the project")
    out = git(repo, "ls-remote", "--heads", str(clone), f"refs/heads/{branch}")
    if not out.strip():
        return None
    git(
        repo,
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-write-fetch-head",
        str(clone),
        f"refs/heads/{branch}:refs/heads/{branch}",
    )
    return git(repo, "rev-parse", f"refs/heads/{branch}").strip()


def commits_between(repo: Path, base: str, branch: str) -> list[str]:
    out = git(repo, "log", "--format=%s", f"{base}..refs/heads/{branch}")
    return [line for line in out.splitlines() if line.strip()]


def files_between(repo: Path, base: str, branch: str) -> list[str]:
    out = git(repo, "diff", "--name-only", "--no-ext-diff", "--no-textconv", f"{base}...refs/heads/{branch}")
    return [line for line in out.splitlines() if line.strip()]


def diff(repo: Path, base: str, branch: str, max_bytes: int = 400_000) -> tuple[str, bool]:
    """Unified diff of the run's branch against its base. (text, truncated)."""
    out = git(
        repo,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--stat",
        "--patch",
        f"{base}...refs/heads/{branch}",
    )
    data = out.encode("utf-8", "replace")
    if len(data) <= max_bytes:
        return out, False
    return data[:max_bytes].decode("utf-8", "ignore"), True


def merge(repo: Path, branch: str, expected_branch: str | None, message: str) -> str:
    """Merge the run branch into the currently checked-out branch. Refuses on a dirty tree or wrong branch."""
    state = repo_state(repo)
    if state.branch is None:
        raise GitError("the project is on a detached HEAD; check out a branch first")
    if expected_branch and state.branch != expected_branch:
        raise GitError(f"the project is on {state.branch}, but the run started from {expected_branch}")
    if state.dirty_files:
        raise GitError(
            f"uncommitted changes in {', '.join(state.dirty_files[:5])}; commit or stash them first"
        )
    proc = subprocess.run(
        [*_BASE, "-C", str(repo), "merge", "--no-ff", "--no-edit", "-m", message, f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
        env=_env(),
    )
    if proc.returncode != 0:
        git(repo, "merge", "--abort", check=False)
        raise GitError(f"merge failed and was aborted: {(proc.stdout + proc.stderr).strip()[-400:]}")
    return git(repo, "rev-parse", "HEAD").strip()


def delete_branch(repo: Path, branch: str) -> None:
    if branch_exists(repo, branch):
        git(repo, "branch", "-D", branch)
