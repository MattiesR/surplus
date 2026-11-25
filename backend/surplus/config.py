"""Filesystem locations and runtime settings.

Data lives under ~/.local/share/surplus, config and secrets under ~/.config/surplus (plan §3).
Every path can be overridden through an environment variable, which tests use for isolation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


@dataclass(frozen=True)
class Paths:
    data_dir: Path
    config_dir: Path
    claude_dir: Path
    projects_root: Path = Path.home() / "surplus-projects"
    extra_log_dirs: tuple[Path, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> Paths:
        home = Path.home()
        extra = os.environ.get("SURPLUS_EXTRA_LOG_DIRS", "")
        return cls(
            data_dir=_env_path("SURPLUS_DATA_DIR", home / ".local/share/surplus"),
            config_dir=_env_path("SURPLUS_CONFIG_DIR", home / ".config/surplus"),
            claude_dir=_env_path("SURPLUS_CLAUDE_DIR", home / ".claude"),
            projects_root=_env_path("SURPLUS_PROJECTS_ROOT", home / "surplus-projects"),
            extra_log_dirs=tuple(Path(p).expanduser() for p in extra.split(os.pathsep) if p),
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "surplus.sqlite3"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def log_roots(self) -> tuple[Path, ...]:
        """Directories that hold Claude Code session JSONL files."""
        return (self.claude_dir / "projects", *self.extra_log_dirs)

    @property
    def statusline_spool(self) -> Path:
        return self.data_dir / "spool" / "statusline.jsonl"

    @property
    def api_token_file(self) -> Path:
        return self.config_dir / "api_token"

    @property
    def secrets_file(self) -> Path:
        return self.config_dir / "secrets.env"

    @property
    def runs_dir(self) -> Path:
        """Per-run working data: the agent's git clone, its Claude config dir, the live log, test output."""
        return self.data_dir / "runs"

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.statusline_spool.parent.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        # Owner-only, because the config dir holds the API token and secrets.env.
        self.config_dir.chmod(0o700)


@dataclass(frozen=True)
class Settings:
    """Tunables. Persisted user overrides live in the `setting` table (later phases)."""

    host: str = "127.0.0.1"
    port: int = 8765
    claude_bin: str = "claude"
    tick_seconds: int = 60
    probe_interval_seconds: int = 15 * 60
    probe_timeout_seconds: int = 45
    # A reading older than this makes the source "stale" for the dashboard and doctor.
    stale_after_seconds: int = 60 * 60
    # Runner (plan §8, §9). Hard stops are checked before a run and live on every rate_limit_event.
    runner_image: str = "localhost/surplus-runner:latest"
    run_max_five_hour_pct: float = 85.0
    run_max_weekly_pct: float = 90.0
    run_timeout_minutes: int = 45
    test_timeout_minutes: int = 15
    default_max_turns: int = 40
    max_concurrent_runs: int = 1
    run_memory: str = "4g"
    run_cpus: float = 2.0
    run_stop_grace_seconds: int = 60
    # Empty = open internet (plan §9.2). A list restricts agents to these hosts (ADR 0007), e.g.
    # SURPLUS_EGRESS_ALLOW="api.anthropic.com,pypi.org,files.pythonhosted.org,registry.npmjs.org"
    egress_allow: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, **overrides: Any) -> Settings:
        """Defaults, with SURPLUS_CLAUDE_BIN honoured (tests and e2e point it at a fake CLI)."""
        env: dict[str, Any] = {}
        if os.environ.get("SURPLUS_CLAUDE_BIN"):
            env["claude_bin"] = os.environ["SURPLUS_CLAUDE_BIN"]
        if os.environ.get("SURPLUS_EGRESS_ALLOW"):
            env["egress_allow"] = tuple(
                h.strip() for h in os.environ["SURPLUS_EGRESS_ALLOW"].split(",") if h.strip()
            )
        if os.environ.get("SURPLUS_RUNNER_IMAGE"):
            env["runner_image"] = os.environ["SURPLUS_RUNNER_IMAGE"]
        return replace(cls(), **{**env, **overrides})
