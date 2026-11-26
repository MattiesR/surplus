"""`surplus install`: run surplusd as a systemd *user* service (plan §5 Phase 5, ADR 0019).

The unit holds no secrets: the API token and the subscription token stay in ~/.config/surplus/ and are read
by the server itself. It binds to 127.0.0.1 only (the `serve` default, which refuses anything else). It
deliberately sets no `NoNewPrivileges=`: rootless Podman needs the setuid `newuidmap` helpers.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

UNIT_NAME = "surplus.service"
# Non-secret settings worth carrying into the service; secrets never go into a unit file.
PASSTHROUGH = (
    "SURPLUS_TZ",
    "SURPLUS_EGRESS_ALLOW",
    "SURPLUS_RUNNER_IMAGE",
    "SURPLUS_CLAUDE_BIN",
    "SURPLUS_DATA_DIR",
    "SURPLUS_CONFIG_DIR",
    "SURPLUS_CLAUDE_DIR",
    "SURPLUS_PROJECTS_ROOT",
    "SURPLUS_EXTRA_LOG_DIRS",
)
SECRET_HINTS = ("TOKEN", "SECRET", "PASSWORD", "KEY")


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def surplus_executable() -> Path:
    """The `surplus` script of the environment this command runs in."""
    beside = Path(sys.executable).parent / "surplus"
    if beside.exists():
        return beside
    found = shutil.which("surplus")
    if found:
        return Path(found)
    raise FileNotFoundError("cannot find the surplus executable; run this via `uv run surplus install`")


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_unit(exe: Path, workdir: Path, path_env: str, env: dict[str, str], port: int = 8765) -> str:
    """The unit file text (pure)."""
    lines = [
        "[Unit]",
        "Description=Surplus: put unused Claude subscription capacity to work",
        "Documentation=file://" + str(workdir / "README.md"),
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={shlex.quote(str(exe))} serve --host 127.0.0.1 --port {port}",
        f"WorkingDirectory={workdir}",
        f"Environment=PATH={_quote(path_env)}",
    ]
    for name in sorted(env):
        if name in PASSTHROUGH and not any(h in name for h in SECRET_HINTS):
            lines.append(f"Environment={name}={_quote(env[name])}")
    lines += [
        "Environment=SURPLUS_SERVICE=1",
        "UMask=0077",
        "Restart=on-failure",
        "RestartSec=10",
        "TimeoutStopSec=90",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def service_path_env() -> str:
    """PATH for the service: where `claude`, `podman` and `git` are found, without the shell's extras."""
    wanted = [Path.home() / ".local/bin", Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin")]
    for tool in ("claude", "podman", "git"):
        found = shutil.which(tool)
        if found and Path(found).parent not in wanted:
            wanted.insert(0, Path(found).parent)
    return ":".join(str(p) for p in wanted)


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, timeout=60, check=False
    )


@dataclass(frozen=True)
class ServiceState:
    installed: bool
    enabled: bool
    active: bool
    linger: bool | None  # None: could not tell

    def describe(self) -> str:
        if not self.installed:
            return "not installed (`surplus install` runs it at login)"
        parts = ["enabled" if self.enabled else "disabled", "running" if self.active else "stopped"]
        if self.linger is False:
            parts.append("starts at login (`loginctl enable-linger` would start it at boot)")
        elif self.linger:
            parts.append("starts at boot (linger)")
        return ", ".join(parts)


def service_state() -> ServiceState:
    installed = (unit_dir() / UNIT_NAME).exists()
    if not installed or shutil.which("systemctl") is None:
        return ServiceState(installed, False, False, None)
    enabled = systemctl("is-enabled", UNIT_NAME).stdout.strip() == "enabled"
    active = systemctl("is-active", UNIT_NAME).stdout.strip() == "active"
    linger: bool | None = None
    user = os.environ.get("USER")
    if user and shutil.which("loginctl"):
        out = subprocess.run(
            ["loginctl", "show-user", user, "-p", "Linger", "--value"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        linger = out.stdout.strip() == "yes" if out.returncode == 0 else None
    return ServiceState(installed, enabled, active, linger)
