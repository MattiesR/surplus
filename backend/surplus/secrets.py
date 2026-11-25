"""Read `~/.config/surplus/secrets.env` (plan §9.3). Values are never logged, printed or returned by the API.

Only `CLAUDE_CODE_OAUTH_TOKEN` is ever handed to anything outside Surplus, and only to the official Claude
CLI inside a run container (plan §9.4).
"""

from __future__ import annotations

import stat
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

CLAUDE_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 - a variable name, not a secret


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE lines; `#` comments, blank lines, `export ` prefixes and matching quotes are handled."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def read_secret(path: Path, name: str) -> str | None:
    """One secret, or None. Refuses a file that other users can read."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("secrets.insecure_permissions", path=str(path), mode=oct(st.st_mode & 0o777))
        return None
    value = parse_env(path.read_text(encoding="utf-8")).get(name)
    return value or None


def has_secret(path: Path, name: str) -> bool:
    return read_secret(path, name) is not None
