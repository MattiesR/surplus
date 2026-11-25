from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from surplus.api.app import create_app
from surplus.api.auth import CSRF_COOKIE, CSRF_HEADER, load_or_create_token
from surplus.clock import FakeClock
from surplus.config import Paths, Settings
from surplus.projects.assist import build_prompt, parse_reply

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def client(paths: Paths, clock: FakeClock, tmp_path: Path) -> Iterator[TestClient]:
    fake = tmp_path / "claude-assist"
    reply = json.dumps({"goals": [{"title": "Ship a CLI", "acceptance": ["prints help", "exits 0"]}]})
    envelope = json.dumps({"type": "result", "is_error": False, "result": f"Here you go:\n{reply}"})
    fake.write_text(f"#!{sys.executable}\nimport sys\nprint({envelope!r})\n")
    fake.chmod(0o755)
    p = Paths(paths.data_dir, paths.config_dir, paths.claude_dir, projects_root=tmp_path / "projects")
    app = create_app(p, Settings(claude_bin=str(fake)), clock, background=False)
    with TestClient(app, base_url=BASE) as c:
        token = load_or_create_token(p.api_token_file)
        c.get(f"/auth?token={token}", follow_redirects=False)
        c.headers[CSRF_HEADER] = c.cookies[CSRF_COOKIE]
        yield c


def _create(client: TestClient, **extra: object) -> dict[str, object]:
    body = {
        "name": "My Cool Idea",
        "description": "Turns CSV into charts.",
        "goals": [{"title": "Ship a CLI", "acceptance": ["prints help"]}],
        "test_command": "uv run pytest -q",
        **extra,
    }
    r = client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    data: dict[str, object] = r.json()
    return data


def test_create_project_writes_files(client: TestClient, tmp_path: Path) -> None:
    """Phase 2 DoD part 1: create a project in the UI → files on disk are correct."""
    detail = _create(client)
    folder = tmp_path / "projects" / "my-cool-idea"
    assert detail["id"] == "my-cool-idea"
    assert detail["path"] == str(folder)
    assert (folder / ".git").is_dir()
    for name in ("project.md", "goals.md", "backlog.md", "decisions.md"):
        assert (folder / ".surplus" / name).is_file()
    assert detail["goals"] == [{"id": "G1", "title": "Ship a CLI", "acceptance": ["prints help"]}]
    cards = client.get("/api/projects").json()
    assert [c["id"] for c in cards] == ["my-cool-idea"]
    assert cards[0]["errors"] == []


def test_duplicate_and_unsafe_create(client: TestClient) -> None:
    _create(client)
    assert client.post("/api/projects", json={"name": "My Cool Idea"}).status_code == 400
    r = client.post("/api/projects", json={"name": "Evil", "mode": "existing", "path": "/etc"})
    assert r.status_code == 400
    assert "system location" in r.json()["detail"]


def test_edit_backlog_via_api(client: TestClient) -> None:
    _create(client)
    detail = client.get("/api/projects/my-cool-idea").json()
    h = detail["docs"]["backlog"]["hash"]
    r = client.post(
        "/api/projects/my-cool-idea/backlog",
        json={"base_hash": h, "title": "Add export", "priority": 1, "size": "S"},
    )
    assert r.status_code == 201
    h2 = r.json()["hash"]
    # Stale hash → 409 conflict, never a silent overwrite.
    assert (
        client.post("/api/projects/my-cool-idea/backlog", json={"base_hash": h, "title": "x"}).status_code
        == 409
    )
    r = client.patch("/api/projects/my-cool-idea/backlog/T-001", json={"base_hash": h2, "done": True})
    assert r.status_code == 200
    tasks = client.get("/api/projects/my-cool-idea").json()["backlog"]
    assert tasks == [
        {
            "id": "T-001",
            "title": "Add export",
            "priority": 1,
            "size": "S",
            "done": True,
            "acceptance": None,
            "flags": [],
            "notes": [],
        }
    ]


def test_invalid_backlog_write_reports_line(client: TestClient) -> None:
    _create(client)
    doc = client.get("/api/projects/my-cool-idea").json()["docs"]["backlog"]
    r = client.put(
        "/api/projects/my-cool-idea/docs/backlog",
        json={"content": doc["content"] + "- [ ] T-1 (P1) bad\n", "base_hash": doc["hash"]},
    )
    assert r.status_code == 422
    assert r.json()["line"] == 4


def test_settings_patch(client: TestClient) -> None:
    _create(client)
    h = client.get("/api/projects/my-cool-idea").json()["docs"]["project"]["hash"]
    r = client.patch(
        "/api/projects/my-cool-idea/settings", json={"base_hash": h, "status": "paused", "model": "haiku"}
    )
    assert r.status_code == 200
    d = client.get("/api/projects/my-cool-idea").json()
    assert (d["status"], d["model"]) == ("paused", "haiku")


def test_unknown_project_404(client: TestClient) -> None:
    assert client.get("/api/projects/nope").status_code == 404


def test_assist_uses_cli_reply(client: TestClient) -> None:
    r = client.post("/api/assist/acceptance", json={"name": "X", "goals": ["Ship a CLI"]})
    assert r.status_code == 200
    assert r.json() == {"goals": [{"title": "Ship a CLI", "acceptance": ["prints help", "exits 0"]}]}


def test_assist_prompt_and_reply_parsing() -> None:
    assert "Goals:\n1. A\n2. B" in build_prompt("n", "d", ["A", "B"])
    assert parse_reply('```json\n{"goals": [{"title": "t"}]}\n```')[0].acceptance == []


def test_system_check(client: TestClient) -> None:
    data = client.get("/api/system").json()
    assert set(data) == {"projects_root", "podman", "runner_image", "token", "detail"}
    assert data["token"] is False  # only presence is ever reported, never the value
