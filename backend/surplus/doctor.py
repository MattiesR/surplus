"""`surplus doctor` checks (plan §13). Each check is independent and returns a result, never raises."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum

from surplus.clock import SystemClock
from surplus.collector.jsonl import iter_log_files
from surplus.collector.reset_watcher import MAX_RESET_AGE
from surplus.collector.sources.probe import ProbeError, run_probe
from surplus.collector.store import latest_readings, latest_reset_reading
from surplus.config import Paths, Settings
from surplus.db.session import make_engine, make_sessionmaker, migrate


class Level(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str


def _run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def check_cli(settings: Settings) -> Check:
    path = shutil.which(settings.claude_bin)
    if not path:
        return Check("claude CLI", Level.FAIL, f"`{settings.claude_bin}` not on PATH")
    try:
        out = _run([path, "--version"]).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("claude CLI", Level.FAIL, str(exc))
    return Check("claude CLI", Level.OK, f"{out} ({path})")


def check_auth(settings: Settings) -> Check:
    """Runs the zero-quota get_usage probe: proves the CLI is logged in and limits are readable."""
    try:
        snap = asyncio.run(run_probe(settings.claude_bin, SystemClock(), settings.probe_timeout_seconds))
    except (ProbeError, OSError) as exc:
        return Check("auth + limits probe", Level.FAIL, str(exc))
    if not snap.readings:
        return Check("auth + limits probe", Level.WARN, "logged in, but no plan rate limits are reported")
    buckets = ", ".join(f"{r.bucket} {r.used_pct:.0f}%" for r in snap.readings)
    return Check("auth + limits probe", Level.OK, f"plan={snap.subscription_type}: {buckets}")


def check_podman() -> Check:
    path = shutil.which("podman")
    if not path:
        return Check(
            "podman (rootless)", Level.WARN, "not installed; needed from Phase 3 (`sudo apt install podman`)"
        )
    try:
        res = _run([path, "info", "--format", "{{.Host.Security.Rootless}}"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("podman (rootless)", Level.FAIL, str(exc))
    if res.returncode != 0:
        return Check("podman (rootless)", Level.FAIL, res.stderr.strip()[:300])
    rootless = res.stdout.strip() == "true"
    return Check("podman (rootless)", Level.OK if rootless else Level.FAIL, f"rootless={rootless}")


def check_runner(paths: Paths, settings: Settings) -> Check:
    """Runner image, agent network and setup token (only its presence; the value is never read out)."""
    from surplus.runner.container import AGENT_NETWORK
    from surplus.secrets import CLAUDE_TOKEN, has_secret

    name = "agent runner"
    podman = shutil.which("podman")
    if not podman:
        return Check(name, Level.WARN, "needs Podman")
    problems = []
    try:
        if _run([podman, "image", "exists", settings.runner_image]).returncode != 0:
            problems.append(f"image {settings.runner_image} missing (`surplus runner build`)")
        net = _run([podman, "network", "inspect", AGENT_NETWORK, "--format", "{{.Internal}}"])
        if net.returncode == 0 and net.stdout.strip() != "true":
            problems.append(f"network {AGENT_NETWORK} exists but is NOT internal: remove it")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(name, Level.FAIL, str(exc))
    if not has_secret(paths.secrets_file, CLAUDE_TOKEN):
        problems.append(f"no {CLAUDE_TOKEN} in {paths.secrets_file} (0600); run `claude setup-token`")
    if problems:
        return Check(name, Level.WARN, "; ".join(problems))
    return Check(name, Level.OK, f"image {settings.runner_image}, token present, network {AGENT_NETWORK}")


def check_logs(paths: Paths) -> Check:
    roots = [r for r in paths.log_roots if r.is_dir()]
    if not roots:
        return Check("log dir", Level.FAIL, f"none of {[str(r) for r in paths.log_roots]} exists")
    unreadable = [r for r in roots if not os.access(r, os.R_OK | os.X_OK)]
    if unreadable:
        return Check("log dir", Level.FAIL, f"not readable: {unreadable}")
    n = sum(1 for _ in iter_log_files(tuple(roots)))
    return Check("log dir", Level.OK, f"{n} session logs under {', '.join(map(str, roots))}")


def check_percent_source(paths: Paths) -> Check:
    if not paths.db_path.exists():
        return Check("percent source fresh", Level.WARN, "no database yet; start `surplus serve`")
    migrate(paths.db_url)
    engine = make_engine(paths.db_url)
    try:
        with make_sessionmaker(engine)() as session:
            last = latest_reset_reading(session)
            latest = latest_readings(session)
    finally:
        engine.dispose()
    if last is None:
        return Check("percent source fresh", Level.WARN, "no reading with a reset time yet")
    age = SystemClock().now() - last
    level = Level.OK if age <= MAX_RESET_AGE else Level.WARN
    summary = ", ".join(f"{r.bucket} {r.used_pct:.0f}% ({r.source.value})" for r in latest)
    return Check(
        "percent source fresh",
        level,
        f"last reset time read {int(age.total_seconds() // 60)} min ago; {summary}",
    )


def check_channels() -> Check:
    return Check("notification channels", Level.WARN, "none configured (Phase 6)")


def check_service() -> Check:
    from surplus.install import service_state

    state = service_state()
    level = Level.OK if state.installed and state.enabled and state.active else Level.WARN
    return Check("service", level, state.describe())


def run_all(paths: Paths, settings: Settings, probe: bool = True) -> list[Check]:
    checks = [check_cli(settings)]
    if probe and checks[0].level is Level.OK:
        checks.append(check_auth(settings))
    checks += [
        check_podman(),
        check_runner(paths, settings),
        check_logs(paths),
        check_percent_source(paths),
        check_channels(),
        check_service(),
    ]
    return checks
