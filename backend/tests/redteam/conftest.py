"""Shared fixtures for the red-team suites: a real run container, started exactly as the run service does."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from surplus.config import Settings
from surplus.runner import gitops
from surplus.runner.container import BASE_ENV, PodmanRuntime, RunContainerSpec, run_args


def host_gateway() -> str:
    out = subprocess.run(
        ["ip", "route", "show", "default"], capture_output=True, text=True, check=False
    ).stdout
    m = re.search(r"default via (\S+)", out)
    return m.group(1) if m else "192.168.0.1"


@dataclass
class RunBox:
    name: str
    project: Path  # the user's project (never mounted)
    workspace: Path  # the run clone (mounted at /workspace)
    config: Path
    marker: str  # unique name for files the attacks try to create

    def sh(
        self, script: str, timeout: float = 60, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        flags: list[str] = []
        for k in env or {}:
            flags += ["--env", k]
        return subprocess.run(
            ["podman", "exec", *flags, self.name, "bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, **(env or {})},
        )


@pytest.fixture(scope="module")
def box(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RunBox]:
    if shutil.which("podman") is None:
        pytest.skip("podman is not installed")
    settings = Settings.from_env()
    runtime = PodmanRuntime()
    if not asyncio.run(runtime.image_exists(settings.runner_image)):
        pytest.skip(f"runner image {settings.runner_image} missing: make runner-image")
    claude = shutil.which("claude")
    base = tmp_path_factory.mktemp("redteam")
    project = base / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(project)], check=True)
    (project / "README.md").write_text("redteam\n")
    subprocess.run(
        ["git", "-C", str(project), "-c", "user.name=t", "-c", "user.email=t@t.invalid", "add", "-A"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(project),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t.invalid",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    workspace = base / "run" / "repo"
    gitops.prepare_clone(project, workspace, "surplus/T-000-redteam")
    config = base / "run" / "claude"
    config.mkdir(parents=True)
    marker = f"surplus-redteam-{uuid.uuid4().hex[:8]}"
    name = f"surplus-redteam-{uuid.uuid4().hex[:6]}"
    asyncio.run(runtime.ensure_network())
    asyncio.run(runtime.ensure_proxy(settings.runner_image))
    spec = RunContainerSpec(
        name=name,
        run_id="redteam",
        instance="redteam",
        image=settings.runner_image,
        workspace=workspace,
        claude_config=config,
        claude_bin=Path(claude).resolve() if claude else Path("/usr/bin/true"),
        lifetime_seconds=1800,
        env=dict(BASE_ENV),
    )
    subprocess.run(["podman", *run_args(spec)], check=True, capture_output=True, timeout=120)
    try:
        yield RunBox(name, project, workspace, config, marker)
    finally:
        subprocess.run(["podman", "rm", "--force", "--time", "2", name], capture_output=True, check=False)
