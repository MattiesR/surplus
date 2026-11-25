"""Unit tests for the runner's pure parts: state machine, report, stream, guard, prompt, proxy policy,
container arguments and the secrets reader."""

from __future__ import annotations

import asyncio
import ipaddress
import itertools
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fixtures.fake_runtime import init, limits, report_block, result, text, tool
from hypothesis import given
from hypothesis import strategies as st

from surplus.domain.models import LimitReading, ReadingSource
from surplus.projects.backlog import BacklogItem, Size
from surplus.projects.documents import Decision, Goal
from surplus.runner import egress_proxy as px
from surplus.runner import prompt, report
from surplus.runner.container import BASE_ENV, TOKEN_ENV, ContainerError, RunContainerSpec, run_args
from surplus.runner.guard import Limits, budget_pct, over_limit
from surplus.runner.state import ALLOWED, TERMINAL, RunState, TransitionError, recover, transition
from surplus.runner.stream import StreamState, handle_line, spent_pct
from surplus.secrets import parse_env, read_secret

TS = datetime(2026, 11, 25, 21, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------- state machine


def test_main_path_is_legal() -> None:
    path = [
        "queued",
        "preparing",
        "running",
        "wrapping_up",
        "verifying",
        "reporting",
        "awaiting_feedback",
        "done",
    ]
    for a, b in itertools.pairwise(path):
        assert transition(RunState(a), RunState(b)) == RunState(b)


def test_illegal_moves_raise() -> None:
    with pytest.raises(TransitionError):
        transition(RunState.QUEUED, RunState.RUNNING)  # skipping preparing
    with pytest.raises(TransitionError):
        transition(RunState.RUNNING, RunState.BUDGET_EXHAUSTED)  # budget stops after start still report
    with pytest.raises(TransitionError):
        transition(RunState.AWAITING_FEEDBACK, RunState.FAILED)


def test_terminal_states_have_no_exits() -> None:
    for s in TERMINAL:
        assert ALLOWED[s] == frozenset()


def test_recover() -> None:
    assert recover(RunState.RUNNING) is RunState.FAILED
    assert recover(RunState.AWAITING_FEEDBACK) is None
    assert recover(RunState.DONE) is None


# ---------------------------------------------------------------- report


def test_extract_fenced_report() -> None:
    ex = report.extract_report(report_block())
    assert ex.report is not None and ex.report.questions[0].id == "Q1"


def test_extract_bare_json_and_last_wins() -> None:
    first = json.dumps({"summary": "old"})
    last = json.dumps({"summary": "new", "tests": "fail"})
    ex = report.extract_report(f"Draft: {first}\nFinal: {last}")
    assert ex.report is not None and ex.report.summary == "new" and ex.report.tests == "fail"


def test_extract_handles_braces_in_strings() -> None:
    body = json.dumps({"summary": 'use {curly} and "quotes" }', "done": ["a}b"]})
    ex = report.extract_report(f"text {body} more")
    assert ex.report is not None and ex.report.done == ["a}b"]


def test_extract_errors_are_specific() -> None:
    assert "empty" in (report.extract_report("").error or "")
    assert "no JSON" in (report.extract_report("just prose").error or "")
    bad = report.extract_report('```json\n{"summary": "x", "tests": "maybe"}\n```')
    assert bad.report is None and "tests" in (bad.error or "")


def test_suggestion_titles_are_single_line_and_backlog_safe() -> None:
    s = report.Suggestion(title="Add\nthing — acceptance: x", size="S")
    assert "\n" not in s.title and " — acceptance:" not in s.title
    BacklogItem(id="T-001", title=s.title, size=Size.S).validate()


@given(st.text(max_size=300))
def test_extract_never_crashes(t: str) -> None:
    report.extract_report(t)


def test_fallback_and_render() -> None:
    rep = report.fallback_report(["feat: a", "fix: b"], ["a.py", "b.py"], "pass")
    assert rep.tests == "pass" and "2 commit(s)" in rep.summary
    facts = report.RunFacts(
        run_id="r1",
        project_id="p",
        task_id="T-001",
        task_title="Do it",
        model="sonnet",
        branch="surplus/T-001",
        started_at=TS,
        ended_at=TS,
        state="awaiting_feedback",
        exit_reason="completed",
        verified_tests="pass",
        spent_pct_est=1.5,
        commits=2,
        report_source="fallback",
    )
    md = report.render_run_md(facts, rep, "1 passed\n```weird```")
    assert md.startswith("# Run r1 · T-001 Do it\n")
    assert "````" in md  # test output containing ``` gets a longer fence
    assert "1.5 pp" in md


def test_schema_is_exported() -> None:
    schema = report.report_json_schema()
    assert set(schema["properties"]) >= {
        "summary",
        "done",
        "not_done",
        "tests",
        "questions",
        "next_suggestions",
    }  # type: ignore[call-overload]


# ---------------------------------------------------------------- stream


def test_stream_lines_become_log_entries() -> None:
    st_ = StreamState()
    out = [
        handle_line(st_, line, TS)
        for line in [
            init(),
            text("hi"),
            tool("Bash", command="pytest -q"),
            limits(30, 7),
            result("done", turns=4),
            "",
            "garbage",
        ]
    ]
    kinds = [e.kind for r in out for e in r.entries]
    assert kinds == ["system", "text", "tool", "limit", "result", "error"]
    assert out[2].entries[0].text == "Bash: pytest -q"
    assert st_.session_id == "sess-1" and st_.num_turns == 4 and st_.final_text == "done" and st_.finished
    assert {r.bucket for r in out[3].readings} == {"five_hour", "seven_day"}
    assert spent_pct(st_, "five_hour", before=25.0) == pytest.approx(5.0)
    assert st_.bad_lines == 1


def test_recorded_container_stream_parses() -> None:
    st_ = StreamState()
    for line in (FIXTURES / "runner" / "container_stream.jsonl").read_text().splitlines():
        handle_line(st_, line, TS)
    assert st_.result_subtype == "success" and st_.final_text == "OK"
    assert st_.readings["five_hour"].used_pct == 45.0


def test_tool_results_are_clipped() -> None:
    st_ = StreamState()
    rec = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "content": "x" * 5000, "is_error": True}]},
    }
    entry = handle_line(st_, json.dumps(rec), TS).entries[0]
    assert entry.kind == "tool_result" and entry.is_error and len(entry.text) <= 800


# ---------------------------------------------------------------- guard


def reading(bucket: str, pct: float, status: str | None = "allowed") -> LimitReading:
    return LimitReading(
        ts=TS, bucket=bucket, used_pct=pct, resets_at=None, source=ReadingSource.STREAM, status=status
    )


def test_guard() -> None:
    lim = Limits(85, 90)
    assert over_limit([reading("five_hour", 84.9), reading("seven_day", 50)], lim) is None
    assert "five_hour" in (over_limit([reading("five_hour", 85)], lim) or "")
    assert "seven_day_opus" in (over_limit([reading("seven_day_opus", 91)], lim) or "")
    assert "rejected" in (over_limit([reading("five_hour", 10, "rejected")], lim) or "")
    assert over_limit([reading("something_new", 99)], lim) is None  # unknown buckets have no cap
    assert budget_pct([reading("five_hour", 25)], lim) == 60
    assert budget_pct([reading("seven_day", 25)], lim) is None


# ---------------------------------------------------------------- prompt


def test_compose_and_branch_name() -> None:
    task = BacklogItem(
        id="T-007",
        title="Add CSV export!",
        priority=1,
        size=Size.S,
        acceptance="exports a .csv",
        notes=["  - use the stdlib"],
    )
    out = prompt.compose(
        name="Demo",
        description="A demo.",
        goals=[Goal("G1", "Ship", ["works"], "")],
        task=task,
        decisions=[
            Decision("2026-11-20 · T-007", "CSV or TSV?", "CSV", ""),
            Decision("older", None, None, "free text"),
        ],
        recent_runs=[("2026-11-24T1000Z-r3.md", "# Run r3\n\n## Summary\n\nDid the parser.\n\n## Done\n")],
        branch="surplus/T-007-add-csv-export",
        test_command="uv run pytest -q",
        max_turns=30,
    )
    assert "# Your task: T-007 — Add CSV export!" in out.prompt
    assert "Q: CSV or TSV? → A: CSV" in out.prompt
    assert out.prompt.index("CSV or TSV") < out.prompt.index("free text")  # related decision first
    assert "r3: Did the parser." in out.prompt
    assert "use the stdlib" in out.prompt
    assert "`uv run pytest -q`" in out.system_append and "at most 30 turns" in out.system_append
    assert '"next_suggestions"' in out.system_append
    assert prompt.branch_name(task) == "surplus/T-007-add-csv-export"
    assert prompt.branch_name(BacklogItem(id="T-001", title="???")) == "surplus/T-001"


# ---------------------------------------------------------------- egress proxy policy


@pytest.mark.parametrize(
    ("addr", "ok"),
    [
        ("93.184.215.14", True),
        ("2606:4700::6810:84e5", True),
        ("192.168.0.1", False),
        ("10.0.0.5", False),
        ("172.16.3.4", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),  # cloud metadata
        ("100.64.0.1", False),  # CGNAT / Tailscale
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("255.255.255.255", False),
        ("::1", False),
        ("fe80::1", False),
        ("fd00::1", False),
        ("::ffff:192.168.0.1", False),  # IPv4-mapped private
        ("::ffff:93.184.215.14", True),
        ("2002:c0a8:0001::1", False),  # 6to4 wrapping 192.168.0.1
        ("64:ff9b::c0a8:1", False),  # NAT64 wrapping 192.168.0.1
    ],
)
def test_address_policy(addr: str, ok: bool) -> None:
    assert px.address_allowed(ipaddress.ip_address(addr)) is ok


def test_extra_deny_list() -> None:
    deny = px.parse_deny_list("93.184.215.14/32, 2606:4700::/32")
    assert not px.address_allowed(ipaddress.ip_address("93.184.215.14"), deny)
    assert not px.address_allowed(ipaddress.ip_address("2606:4700::1"), deny)
    assert px.address_allowed(ipaddress.ip_address("1.1.1.1"), deny)


def test_host_allowlist() -> None:
    allow = px.parse_allow_list("api.anthropic.com, *.pythonhosted.org,PyPI.org.")
    assert px.host_allowed("anything.example", ())  # no list: open internet (plan §9.2)
    assert px.host_allowed("api.anthropic.com", allow)
    assert px.host_allowed("files.pythonhosted.org", allow)
    assert px.host_allowed("pypi.org", allow)
    assert not px.host_allowed("pythonhosted.org.evil.com", allow)
    assert not px.host_allowed("evilpypi.org", allow)
    assert not px.host_allowed("example.com", allow)


async def test_proxy_enforces_the_allowlist() -> None:
    proxy = px.Proxy(allow=("api.anthropic.com",))
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        res = await _proxy_request(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        assert res.startswith(b"HTTP/1.1 403") and b"allowlist" in res


def test_parse_target() -> None:
    assert px.parse_target("CONNECT", "example.com:443") == ("example.com", 443, None)
    assert px.parse_target("CONNECT", "[2606:4700::1]:443") == ("2606:4700::1", 443, None)
    assert px.parse_target("GET", "http://example.com/a?b=1") == ("example.com", 80, "/a?b=1")
    assert px.parse_target("GET", "http://example.com:8080") == ("example.com", 8080, "/")
    for method, target in [
        ("GET", "/relative"),
        ("GET", "http://user:pw@example.com/"),
        ("CONNECT", "nohost"),
    ]:
        with pytest.raises(px.RefusedError):
            px.parse_target(method, target)


async def _proxy_request(port: int, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(4096), 5)
    writer.close()
    return data


async def test_proxy_refuses_private_targets_and_odd_ports() -> None:
    proxy = px.Proxy()
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        lan = await _proxy_request(port, b"CONNECT 192.168.0.1:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        assert lan.startswith(b"HTTP/1.1 403") and b"non-public" in lan
        loop = await _proxy_request(port, b"GET http://localhost/ HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert loop.startswith(b"HTTP/1.1 403")
        ssh = await _proxy_request(port, b"CONNECT example.com:22 HTTP/1.1\r\n\r\n")
        assert ssh.startswith(b"HTTP/1.1 403") and b"port 22" in ssh
        junk = await _proxy_request(port, b"HELLO\r\n\r\n")
        assert junk.startswith(b"HTTP/1.1 400")


async def test_proxy_tunnels_to_an_allowed_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowed path, with resolution pointed at a local echo server (the policy itself is tested above)."""

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await reader.read(100))
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]
    proxy = px.Proxy()

    async def fake_resolve(host: str, port: int) -> ipaddress.IPv4Address:
        return ipaddress.IPv4Address("127.0.0.1")

    monkeypatch.setattr(proxy, "resolve", fake_resolve)
    monkeypatch.setattr(px, "ALLOWED_PORTS", frozenset({up_port}))
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with upstream, server:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT public.example:{up_port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        writer.write(b"ping")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(4), 5) == b"ping"
        writer.close()


# ---------------------------------------------------------------- container arguments


def spec(**env: str) -> RunContainerSpec:
    return RunContainerSpec(
        name="surplus-run-x-1",
        run_id="r1",
        instance="x",
        image="img",
        workspace=Path("/w"),
        claude_config=Path("/c"),
        claude_bin=Path("/b/claude"),
        lifetime_seconds=60,
        env={**BASE_ENV, **env},
    )


def test_run_args_are_locked_down() -> None:
    args = run_args(spec())
    joined = " ".join(args)
    for flag in (
        "--read-only",
        "--cap-drop=all",
        "--userns=keep-id",
        "no-new-privileges",
        "--network surplus-agents",
    ):
        assert flag in joined
    assert "/b/claude:/usr/local/bin/claude:ro" in args
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--volume"]
    assert len(mounts) == 3  # workspace, per-run config, CLI binary: nothing else from the host
    assert not any(".ssh" in m or m.startswith(str(Path.home())) for m in mounts)
    assert args[-3:] == ["img", "sleep", "60"]


def test_run_args_refuse_the_token() -> None:
    with pytest.raises(ContainerError):
        run_args(spec(**{TOKEN_ENV: "secret"}))


# ---------------------------------------------------------------- secrets


def test_parse_env() -> None:
    text_ = "# c\nexport A=1\nB = 'two'\nC=\"three\"\nnot a line\n=x\nD=\n"
    assert parse_env(text_) == {"A": "1", "B": "two", "C": "three", "D": ""}


def test_read_secret_refuses_loose_permissions(tmp_path: Path) -> None:
    f = tmp_path / "secrets.env"
    f.write_text("CLAUDE_CODE_OAUTH_TOKEN=abc\n")
    os.chmod(f, 0o600)
    assert read_secret(f, "CLAUDE_CODE_OAUTH_TOKEN") == "abc"
    os.chmod(f, 0o644)
    assert read_secret(f, "CLAUDE_CODE_OAUTH_TOKEN") is None
    assert read_secret(tmp_path / "missing", "X") is None
