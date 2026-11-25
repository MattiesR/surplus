from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from surplus.clock import FakeClock
from surplus.config import Paths
from surplus.db.session import make_engine, make_sessionmaker, migrate

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 25, 20, 0, tzinfo=UTC)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        claude_dir=tmp_path / "claude",
    )
    p.ensure()
    (p.claude_dir / "projects").mkdir(parents=True)
    return p


@pytest.fixture
def sm(paths: Paths) -> Iterator[sessionmaker[Session]]:
    migrate(paths.db_url)
    engine = make_engine(paths.db_url)
    yield make_sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(T0)


def assistant_line(
    msg_id: str,
    ts: str = "2026-11-25T19:30:00.000Z",
    session: str = "s1",
    model: str = "claude-opus-5-5",
    usage: dict[str, Any] | None = None,
) -> str:
    """One assistant line in the Claude Code log format (spike 1)."""
    rec = {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": session,
        "requestId": f"req_{msg_id}",
        "isSidechain": False,
        "entrypoint": "cli",
        "message": {
            "id": msg_id,
            "model": model,
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "usage": usage
            or {
                "input_tokens": 2,
                "output_tokens": 100,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 5000,
            },
        },
    }
    return json.dumps(rec)


def other_line(kind: str = "user") -> str:
    return json.dumps({"type": kind, "sessionId": "s1", "message": {"role": "user", "content": "x"}})


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())
