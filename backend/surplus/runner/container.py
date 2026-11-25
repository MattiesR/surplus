"""Podman adapter (plan §9.2, ADRs 0003, 0005, 0006) — the only module that talks to `podman`.

Every run gets its own container:
- rootless Podman, `--userns=keep-id` (the agent is the host user's uid, never root), read-only root fs,
  `--cap-drop=all`, `no-new-privileges`, memory/CPU/pids caps, and a `sleep` entrypoint that ends the
  container by itself if Surplus dies;
- mounts: the run's git clone at /workspace (rw), the run's own Claude config dir (rw) and the host's Claude
  CLI binary (ro). Nothing else from the host: no home, no `~/.claude`, no sockets;
- network: the internal `surplus-agents` network only; the `surplus-egress` proxy is the only way out.

Secrets: only `CLAUDE_CODE_OAUTH_TOKEN`, and only for the `claude` exec. It is passed as `-e NAME` (no value),
so Podman takes the value from the child process environment: it never appears on a command line and is
not stored in the container's config.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import structlog

log = structlog.get_logger(__name__)

AGENT_NETWORK = "surplus-agents"
PROXY_NAME = "surplus-egress"
PROXY_PORT = 3128
PROXY_URL = f"http://{PROXY_NAME}:{PROXY_PORT}"
PROXY_SOURCE = Path(__file__).with_name("egress_proxy.py")
RUN_LABEL = "org.surplus.run"
# Which Surplus installation (data dir) owns a container; tests and the real daemon never touch each other's.
INSTANCE_LABEL = "org.surplus.instance"
WORKSPACE = "/workspace"
CLAUDE_CONFIG = "/cfg"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 - a variable name, not a secret

# Fixed, non-secret environment for every run container.
BASE_ENV: dict[str, str] = {
    "CLAUDE_CONFIG_DIR": CLAUDE_CONFIG,
    "HTTPS_PROXY": PROXY_URL,
    "HTTP_PROXY": PROXY_URL,
    "https_proxy": PROXY_URL,
    "http_proxy": PROXY_URL,
    "NO_PROXY": "",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
    "GIT_AUTHOR_NAME": "Surplus agent",
    "GIT_AUTHOR_EMAIL": "agent@surplus.invalid",
    "GIT_COMMITTER_NAME": "Surplus agent",
    "GIT_COMMITTER_EMAIL": "agent@surplus.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}


class ContainerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunContainerSpec:
    name: str
    run_id: str
    instance: str
    image: str
    workspace: Path
    claude_config: Path
    claude_bin: Path
    lifetime_seconds: int
    memory: str = "4g"
    cpus: float = 2.0
    pids: int = 1024
    env: Mapping[str, str] = field(default_factory=lambda: dict(BASE_ENV))


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    output: str  # stdout+stderr, tail-truncated
    timed_out: bool = False


class ExecStream(Protocol):
    def lines(self) -> AsyncIterator[bytes]: ...

    async def wait(self) -> int: ...

    def stderr_tail(self) -> str: ...


class ContainerRuntime(Protocol):
    async def image_exists(self, image: str) -> bool: ...

    async def ensure_network(self) -> None: ...

    async def ensure_proxy(self, image: str, allow: tuple[str, ...] = ()) -> None: ...

    async def start(self, spec: RunContainerSpec) -> None: ...

    async def exec_stream(
        self,
        name: str,
        argv: Sequence[str],
        stdin: bytes,
        secrets: Mapping[str, str],
    ) -> ExecStream: ...

    async def exec(
        self, name: str, argv: Sequence[str], timeout: float, max_output: int = 200_000
    ) -> ExecResult: ...

    async def signal_agent(self, name: str) -> None: ...

    async def remove(self, name: str) -> None: ...

    async def exists(self, name: str) -> bool: ...

    async def list_run_containers(self, instance: str) -> dict[str, str]: ...


def instance_id(data_dir: Path) -> str:
    return hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()[:10]


def podman_available() -> bool:
    return shutil.which("podman") is not None


def proxy_source_hash() -> str:
    return hashlib.sha256(PROXY_SOURCE.read_bytes()).hexdigest()[:16]


def host_global_addresses() -> list[str]:
    """The host's own globally routable addresses, so the proxy refuses them too (best effort)."""
    try:
        out = subprocess.run(
            ["ip", "-j", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    addrs: list[str] = []
    for iface in data:
        for info in iface.get("addr_info", []):
            try:
                ip = ipaddress.ip_address(info.get("local", ""))
            except ValueError:
                continue
            if ip.is_global:
                addrs.append(f"{ip}/{ip.max_prefixlen}")
    return addrs


def run_args(spec: RunContainerSpec) -> list[str]:
    """`podman run` arguments for one run container (pure; unit-tested)."""
    args = [
        "run",
        "--detach",
        "--rm",
        "--name",
        spec.name,
        "--label",
        f"{RUN_LABEL}={spec.run_id}",
        "--label",
        f"{INSTANCE_LABEL}={spec.instance}",
        "--network",
        AGENT_NETWORK,
        "--userns=keep-id",
        "--init",  # PID 1 forwards signals and reaps the agent's orphaned processes
        "--read-only",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=2g",  # noqa: S108 - tmpfs inside the container
        "--tmpfs",
        "/home/agent:U",
        "--cap-drop=all",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        spec.memory,
        "--cpus",
        str(spec.cpus),
        "--pids-limit",
        str(spec.pids),
        "--volume",
        f"{spec.workspace}:{WORKSPACE}:rw",
        "--volume",
        f"{spec.claude_config}:{CLAUDE_CONFIG}:rw",
        "--volume",
        f"{spec.claude_bin}:/usr/local/bin/claude:ro",
        "--workdir",
        WORKSPACE,
    ]
    for key, value in sorted(spec.env.items()):
        if key == TOKEN_ENV:
            raise ContainerError("the token must never be part of the container config")
        args += ["--env", f"{key}={value}"]
    return [*args, spec.image, "sleep", str(spec.lifetime_seconds)]


class _PodmanExecStream:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self._stderr: list[bytes] = []
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        async for chunk in self.proc.stderr:
            self._stderr.append(chunk)
            if len(self._stderr) > 200:
                del self._stderr[:100]

    async def lines(self) -> AsyncIterator[bytes]:
        assert self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return
            yield line

    async def wait(self) -> int:
        rc = await self.proc.wait()
        with contextlib.suppress(asyncio.CancelledError):
            await self._stderr_task
        return rc

    def stderr_tail(self) -> str:
        return b"".join(self._stderr).decode("utf-8", "replace")[-4000:]


class PodmanRuntime:
    def __init__(self, podman: str = "podman") -> None:
        self.podman = podman

    async def _run(self, *args: str, timeout: float = 120, check: bool = True) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            self.podman,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_child_env(),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ContainerError(f"podman {args[0]} timed out") from None
        text = out.decode("utf-8", "replace")
        if check and proc.returncode != 0:
            raise ContainerError(f"podman {' '.join(args[:2])} failed: {text.strip()[-500:]}")
        return proc.returncode or 0, text

    async def image_exists(self, image: str) -> bool:
        rc, _ = await self._run("image", "exists", image, check=False)
        return rc == 0

    async def ensure_network(self) -> None:
        rc, _ = await self._run("network", "exists", AGENT_NETWORK, check=False)
        if rc != 0:
            await self._run("network", "create", "--internal", AGENT_NETWORK)
            log.info("runner.network_created", network=AGENT_NETWORK)
        _, out = await self._run("network", "inspect", AGENT_NETWORK)
        info = json.loads(out)
        if not (info and info[0].get("internal")):
            raise ContainerError(f"network {AGENT_NETWORK} exists but is not internal; remove it")

    async def ensure_proxy(self, image: str, allow: tuple[str, ...] = ()) -> None:
        """Start the egress proxy on both networks; restart it when its code or allowlist changed."""
        allow_s = ",".join(allow)
        want = hashlib.sha256(f"{proxy_source_hash()}|{allow_s}".encode()).hexdigest()[:16]
        rc, out = await self._run(
            "inspect",
            PROXY_NAME,
            "--format",
            '{{.State.Running}} {{index .Config.Labels "org.surplus.proxy"}}',
            check=False,
        )
        if rc == 0 and out.split() == ["true", want]:
            return
        await self._run("rm", "--force", "--time", "2", PROXY_NAME, check=False)
        deny = ",".join(host_global_addresses())
        await self._run(
            "run",
            "--detach",
            "--name",
            PROXY_NAME,
            "--label",
            f"org.surplus.proxy={want}",
            "--network",
            "podman",
            "--read-only",
            "--cap-drop=all",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--memory",
            "256m",
            "--pids-limit",
            "128",
            "--volume",
            f"{PROXY_SOURCE}:/opt/egress_proxy.py:ro",
            "--env",
            f"SURPLUS_EGRESS_DENY={deny}",
            "--env",
            f"SURPLUS_EGRESS_ALLOW={allow_s}",
            image,
            "python3",
            "/opt/egress_proxy.py",
        )
        await self._run("network", "connect", AGENT_NETWORK, PROXY_NAME)
        log.info("runner.proxy_started", proxy=PROXY_NAME, hash=want, extra_deny=deny, allow=allow_s or "*")

    async def start(self, spec: RunContainerSpec) -> None:
        await self._run(*run_args(spec), timeout=180)

    async def exec_stream(
        self, name: str, argv: Sequence[str], stdin: bytes, secrets: Mapping[str, str]
    ) -> ExecStream:
        env = _child_env()
        env.update(secrets)
        flags: list[str] = []
        for key in sorted(secrets):
            flags += ["--env", key]  # value comes from `env`, never from argv
        proc = await asyncio.create_subprocess_exec(
            self.podman,
            "exec",
            "--interactive",
            "--workdir",
            WORKSPACE,
            *flags,
            name,
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=8 * 1024 * 1024,  # stream-json lines can be large
        )
        assert proc.stdin is not None
        proc.stdin.write(stdin)
        await proc.stdin.drain()
        proc.stdin.close()
        return _PodmanExecStream(proc)

    async def exec(
        self, name: str, argv: Sequence[str], timeout: float, max_output: int = 200_000
    ) -> ExecResult:
        proc = await asyncio.create_subprocess_exec(
            self.podman,
            "exec",
            "--workdir",
            WORKSPACE,
            name,
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_child_env(),
        )
        chunks: list[bytes] = []
        size = 0

        async def read() -> None:
            nonlocal size
            assert proc.stdout is not None
            async for chunk in proc.stdout:
                chunks.append(chunk)
                size += len(chunk)
                while size > max_output and len(chunks) > 1:
                    size -= len(chunks.pop(0))

        timed_out = False
        try:
            await asyncio.wait_for(read(), timeout)
            rc = await proc.wait()
        except TimeoutError:
            timed_out = True
            proc.kill()
            rc = await proc.wait()
        text = b"".join(chunks).decode("utf-8", "replace")[-max_output:]
        return ExecResult(rc, text, timed_out)

    async def signal_agent(self, name: str) -> None:
        """Ask the agent to stop: SIGINT to the CLI process (exact name match inside the container)."""
        await self._run(
            "exec", name, "pkill", "--signal", "INT", "--exact", "claude", check=False, timeout=20
        )

    async def remove(self, name: str) -> None:
        await self._run("rm", "--force", "--time", "10", name, check=False, timeout=60)

    async def exists(self, name: str) -> bool:
        rc, _ = await self._run("container", "exists", name, check=False)
        return rc == 0

    async def list_run_containers(self, instance: str) -> dict[str, str]:
        """{container name: run id} for every run container of this Surplus instance."""
        _, out = await self._run(
            "ps", "--all", "--filter", f"label={INSTANCE_LABEL}={instance}", "--format", "json", check=False
        )
        try:
            rows = json.loads(out or "[]")
        except json.JSONDecodeError:
            return {}
        found: dict[str, str] = {}
        for row in rows:
            names = row.get("Names") or []
            labels = row.get("Labels") or {}
            if names and RUN_LABEL in labels:
                found[str(names[0])] = str(labels[RUN_LABEL])
        return found


def _child_env() -> dict[str, str]:
    """Minimal environment for podman: never inherit Surplus's own environment wholesale."""
    keep = (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "XDG_RUNTIME_DIR",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "DBUS_SESSION_BUS_ADDRESS",
    )
    return {k: v for k in keep if (v := os.environ.get(k)) is not None}
