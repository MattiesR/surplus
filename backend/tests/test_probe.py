from __future__ import annotations

import sys
from pathlib import Path

import pytest

from surplus.clock import FakeClock
from surplus.collector.sources.probe import PROBE_ARGS, ProbeError, run_probe

from conftest import FIXTURES


@pytest.fixture
def fake_cli(tmp_path: Path) -> str:
    script = tmp_path / "claude"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURES / "fake_claude.py"}" "$@"\n')
    script.chmod(0o755)
    return str(script)


def test_probe_never_sends_a_prompt_and_disables_persistence() -> None:
    assert "--no-session-persistence" in PROBE_ARGS
    assert "--input-format" in PROBE_ARGS


async def test_probe_ok(fake_cli: str, clock: FakeClock) -> None:
    snap = await run_probe(fake_cli, clock, timeout=10)
    assert {r.bucket for r in snap.readings} == {"five_hour", "seven_day"}
    assert snap.readings[0].ts == clock.now()


async def test_probe_cli_exits(fake_cli: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "exit")
    with pytest.raises(ProbeError, match="login"):
        await run_probe(fake_cli, clock, timeout=10)


async def test_probe_error_response(fake_cli: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    with pytest.raises(ProbeError, match="nope"):
        await run_probe(fake_cli, clock, timeout=10)


async def test_probe_timeout(fake_cli: str, clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    with pytest.raises(ProbeError, match="timed out"):
        await run_probe(fake_cli, clock, timeout=1)
