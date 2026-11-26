"""`surplus install` (ADR 0019): the unit file is correct and carries no secrets; install/uninstall flow."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from surplus import install as inst


def test_unit_text_and_no_secrets() -> None:
    env = {
        "SURPLUS_TZ": "Europe/Brussels",
        "SURPLUS_EGRESS_ALLOW": "api.anthropic.com,pypi.org",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-secret",
        "SURPLUS_API_TOKEN": "nope",
        "HOME": "/home/x",
    }
    text = inst.render_unit(Path("/opt/s p/bin/surplus"), Path("/repo"), "/usr/bin:/bin", env, 8765)
    assert "ExecStart='/opt/s p/bin/surplus' serve --host 127.0.0.1 --port 8765" in text
    assert 'Environment=SURPLUS_TZ="Europe/Brussels"' in text
    assert 'Environment=SURPLUS_EGRESS_ALLOW="api.anthropic.com,pypi.org"' in text
    assert "sk-secret" not in text and "SURPLUS_API_TOKEN" not in text and "HOME" not in text
    assert "Environment=SURPLUS_SERVICE=1" in text and "UMask=0077" in text
    assert "NoNewPrivileges" not in text  # rootless Podman needs newuidmap
    assert text.startswith("[Unit]") and "WantedBy=default.target" in text


def test_path_env_contains_the_basics() -> None:
    parts = inst.service_path_env().split(":")
    assert "/usr/bin" in parts and str(Path.home() / ".local/bin") in parts


def test_state_when_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    st = inst.service_state()
    assert not st.installed and "not installed" in st.describe()


def test_install_and_uninstall_with_a_fake_systemctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from surplus.cli import app

    calls: list[tuple[str, ...]] = []

    def fake(*args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        out = {"is-enabled": "enabled", "is-active": "active"}.get(args[0], "")
        return subprocess.CompletedProcess(["systemctl", *args], 0, out, "")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(inst, "systemctl", fake)
    monkeypatch.setattr(inst, "port_in_use", lambda port, host="127.0.0.1": False)
    monkeypatch.setattr(inst.shutil, "which", lambda name: f"/usr/bin/{name}")
    run = CliRunner()

    dry = run.invoke(app, ["install", "--dry-run"])
    assert dry.exit_code == 0 and "[Service]" in dry.output and not (tmp_path / "systemd").exists()

    out = run.invoke(app, ["install"])
    assert out.exit_code == 0, out.output
    unit = tmp_path / "systemd/user/surplus.service"
    assert unit.exists() and "serve --host 127.0.0.1" in unit.read_text()
    assert ("daemon-reload",) in calls and ("enable", "--now", "surplus.service") in calls
    assert "enabled, running" in out.output

    out = run.invoke(app, ["uninstall"])
    assert out.exit_code == 0 and not unit.exists() and ("disable", "--now", "surplus.service") in calls
    assert "not installed" in run.invoke(app, ["uninstall"]).output


def test_install_refuses_when_the_port_is_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from surplus.cli import app

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(inst, "port_in_use", lambda port, host="127.0.0.1": True)
    monkeypatch.setattr(inst.shutil, "which", lambda name: f"/usr/bin/{name}")
    out = CliRunner().invoke(app, ["install"])
    assert out.exit_code == 1 and "already listens" in out.output
    assert not (tmp_path / "systemd/user/surplus.service").exists()


def test_link_prints_the_sign_in_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from surplus.cli import app

    for name in ("DATA", "CONFIG", "CLAUDE"):
        monkeypatch.setenv(f"SURPLUS_{name}_DIR", str(tmp_path / name))
    out = CliRunner().invoke(app, ["link"])
    assert out.output.startswith("http://127.0.0.1:8765/auth?token=")
