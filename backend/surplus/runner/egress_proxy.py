"""Filtering egress proxy for agent containers (ADR 0005). Standard library only.

Agent containers sit on an internal Podman network with no route out. This proxy is the only way out: it
accepts HTTP `CONNECT host:port` and absolute-form plain HTTP requests, resolves the name *itself*, and
connects only when every resolved address is public. LAN, loopback, link-local, CGNAT, multicast, reserved
and address-embedding IPv6 ranges are refused, so neither a private name nor DNS rebinding gets through:
the connection goes to the address that was checked.

The file is bind-mounted into the proxy container and run with the image's python3 (3.11), so it must not
import anything from `surplus` and must stay compatible with Python 3.11.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import signal
import socket
import sys
import time

ALLOWED_PORTS = frozenset({80, 443})
MAX_HEAD = 16 * 1024
HEAD_TIMEOUT = 30.0
IDLE_TIMEOUT = 300.0
MAX_CONNECTIONS = 256

# IPv6 ranges that embed or translate to an IPv4 address (which could be private): refused outright.
_EMBEDDING_V6 = tuple(
    ipaddress.ip_network(n)
    for n in ("2002::/16", "2001::/32", "64:ff9b::/96", "64:ff9b:1::/48", "::/96", "::ffff:0:0/96")
)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_deny_list(raw: str) -> tuple[IPNetwork, ...]:
    """Extra CIDRs to refuse (e.g. the host's own public addresses), comma separated."""
    nets: list[IPNetwork] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return tuple(nets)


def address_allowed(ip: IPAddress, extra_deny: tuple[IPNetwork, ...] = ()) -> bool:
    """True only for globally routable unicast addresses that are not on the extra deny list."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return address_allowed(ip.ipv4_mapped, extra_deny)
        if any(ip in net for net in _EMBEDDING_V6):
            return False
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return not any(ip.version == net.version and ip in net for net in extra_deny)


def parse_allow_list(raw: str) -> tuple[str, ...]:
    """Optional host allowlist: `example.com` (exact) or `*.example.com` (subdomains), comma separated."""
    return tuple(p.strip().lower().rstrip(".") for p in raw.split(",") if p.strip())


def host_allowed(host: str, allow: tuple[str, ...]) -> bool:
    """No allowlist = every public host. Otherwise exact names, or subdomains for `*.` patterns."""
    if not allow:
        return True
    host = host.lower().rstrip(".")
    for pattern in allow:
        if pattern.startswith("*."):
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


class RefusedError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def parse_target(method: str, target: str) -> tuple[str, int, str | None]:
    """(host, port, origin-form path or None for CONNECT) from a request line's method and target."""
    if method == "CONNECT":
        host, sep, port_s = target.rpartition(":")
        if not sep or not port_s.isdigit():
            raise RefusedError(400, "CONNECT target must be host:port")
        return host.strip("[]"), int(port_s), None
    if not target.lower().startswith("http://"):
        raise RefusedError(400, "only absolute http:// URLs or CONNECT are accepted")
    rest = target[len("http://") :]
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        raise RefusedError(400, "credentials in URL are not accepted")
    port = 80
    host = authority
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise RefusedError(400, "bad IPv6 literal")
        host = authority[1:end]
        if authority[end + 1 :].startswith(":"):
            port_s = authority[end + 2 :]
            if not port_s.isdigit():
                raise RefusedError(400, "bad port")
            port = int(port_s)
    elif ":" in authority:
        host, _, port_s = authority.partition(":")
        if not port_s.isdigit():
            raise RefusedError(400, "bad port")
        port = int(port_s)
    return host, port, slash + path if slash else "/"


def _log(event: str, **fields: object) -> None:
    print(json.dumps({"ts": time.time(), "event": event, **fields}), flush=True)


class Proxy:
    def __init__(self, extra_deny: tuple[IPNetwork, ...] = (), allow: tuple[str, ...] = ()) -> None:
        self.extra_deny = extra_deny
        self.allow = allow
        self.slots = asyncio.Semaphore(MAX_CONNECTIONS)

    async def resolve(self, host: str, port: int) -> IPAddress:
        if not host or len(host) > 253:
            raise RefusedError(400, "bad host")
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise RefusedError(502, f"cannot resolve {host}") from exc
        addrs = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
        if not addrs:
            raise RefusedError(502, f"cannot resolve {host}")
        # Every record must be public: a name that also points into the LAN is refused as a whole.
        bad = [str(a) for a in addrs if not address_allowed(a, self.extra_deny)]
        if bad:
            raise RefusedError(403, f"{host} resolves to a non-public address ({bad[0]})")
        return addrs[0]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        async with self.slots:
            try:
                await self._handle(reader, writer)
            except RefusedError as exc:
                _log("refused", peer=str(peer), status=exc.status, reason=exc.reason)
                with contextlib.suppress(Exception):
                    body = (exc.reason + "\n").encode()
                    writer.write(
                        f"HTTP/1.1 {exc.status} RefusedError by Surplus egress proxy\r\n"
                        f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                        "Connection: close\r\n\r\n".encode()
                        + body
                    )
                    await writer.drain()
            except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                pass
            finally:
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        if len(head) > MAX_HEAD:
            raise RefusedError(431, "request head too large")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            raise RefusedError(400, "bad request line")
        method, target, version = parts
        host, port, path = parse_target(method.upper(), target)
        if port not in ALLOWED_PORTS:
            raise RefusedError(403, f"port {port} is not allowed (only 80 and 443)")
        if not host_allowed(host, self.allow):
            raise RefusedError(403, f"{host} is not on the egress allowlist")
        ip = await self.resolve(host, port)
        up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(str(ip), port), 20)
        _log("connect", host=host, ip=str(ip), port=port, method=method)
        if path is None:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            headers = [h for h in lines[1:] if h and not h.lower().startswith("proxy-")]
            up_writer.write(f"{method} {path} {version}\r\n".encode("latin-1"))
            up_writer.write("".join(h + "\r\n" for h in headers).encode("latin-1") + b"\r\n")
            await up_writer.drain()
        try:
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        finally:
            up_writer.close()


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(src.read(65536), IDLE_TIMEOUT)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (TimeoutError, ConnectionError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if dst.can_write_eof():
                dst.write_eof()


async def main() -> None:
    port = int(os.environ.get("SURPLUS_EGRESS_PORT", "3128"))
    proxy = Proxy(
        parse_deny_list(os.environ.get("SURPLUS_EGRESS_DENY", "")),
        parse_allow_list(os.environ.get("SURPLUS_EGRESS_ALLOW", "")),
    )
    server = await asyncio.start_server(proxy.handle, host="0.0.0.0", port=port)  # noqa: S104 - internal net
    _log("listening", port=port, extra_deny=[str(n) for n in proxy.extra_deny], allow=list(proxy.allow))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):  # PID 1 in the container: no default handlers
        loop.add_signal_handler(sig, stop.set)
    async with server:
        await stop.wait()
    _log("stopped")


if __name__ == "__main__":  # pragma: no cover - runs inside the proxy container
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
    sys.exit(0)
