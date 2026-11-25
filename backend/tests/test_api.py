from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from surplus.api.app import create_app
from surplus.api.auth import CSRF_COOKIE, CSRF_HEADER, load_or_create_token
from surplus.clock import FakeClock
from surplus.collector.limits import parse_get_usage
from surplus.config import Paths, Settings
from surplus.domain.models import UsageSnapshot

from conftest import assistant_line, load_fixture

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def client(paths: Paths, clock: FakeClock) -> Iterator[TestClient]:
    payload = load_fixture("probe/get_usage_response.json")["response"]["response"]

    async def fake_probe() -> UsageSnapshot:
        return parse_get_usage(payload, clock.now())

    app = create_app(paths, Settings(), clock, probe=fake_probe, background=False)
    with TestClient(app, base_url=BASE) as c:
        yield c


@pytest.fixture
def authed(client: TestClient, paths: Paths) -> TestClient:
    token = load_or_create_token(paths.api_token_file)
    r = client.get(f"/auth?token={token}", follow_redirects=False)
    assert r.status_code == 303
    return client


def test_health_is_public(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"


def test_api_requires_auth(client: TestClient) -> None:
    assert client.get("/api/status").status_code == 401


def test_bad_token_rejected(client: TestClient) -> None:
    assert client.get("/auth?token=wrong", follow_redirects=False).status_code == 401


def test_dns_rebinding_host_rejected(paths: Paths, clock: FakeClock) -> None:
    app = create_app(paths, Settings(), clock, background=False)
    with TestClient(app, base_url="http://evil.example:8765") as c:
        assert c.get("/api/health").status_code == 421


def test_bearer_token_works(client: TestClient, paths: Paths) -> None:
    token = load_or_create_token(paths.api_token_file)
    r = client.post("/api/probe", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_post_requires_csrf(authed: TestClient) -> None:
    body = {"bucket": "seven_day", "used_pct": 34}
    assert authed.post("/api/limits/manual", json=body).status_code == 403
    csrf = authed.cookies[CSRF_COOKIE]
    r = authed.post("/api/limits/manual", json=body, headers={CSRF_HEADER: csrf})
    assert r.status_code == 201
    assert r.json()["resets_at"] is None  # manual readings never carry a reset time


def test_manual_reading_validation(authed: TestClient) -> None:
    csrf = authed.cookies[CSRF_COOKIE]
    r = authed.post(
        "/api/limits/manual", json={"bucket": "seven_day", "used_pct": 140}, headers={CSRF_HEADER: csrf}
    )
    assert r.status_code == 422


def test_status_after_probe(authed: TestClient, paths: Paths, clock: FakeClock) -> None:
    proj = paths.claude_dir / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(assistant_line("m1", ts="2026-11-25T19:30:00Z") + "\n")
    collector = authed.app.state.collector  # type: ignore[attr-defined]
    collector.ingest_once()
    csrf = authed.cookies[CSRF_COOKIE]
    assert authed.post("/api/probe", headers={CSRF_HEADER: csrf}).status_code == 200

    clock.advance(timedelta(minutes=5))
    status = authed.get("/api/status").json()
    buckets = {b["bucket"]: b for b in status["buckets"]}
    assert buckets["five_hour"]["used_pct"] == 6
    assert buckets["five_hour"]["age_seconds"] == 300
    assert status["buckets"][0]["bucket"] == "five_hour"
    assert status["resets_fresh"] is True
    assert status["week_split"] == {"manual": 1.0, "agent": 0.0, "invisible": 0.0}
    assert {s["name"] for s in status["sources"]} >= {"probe", "cc_log"}

    series = authed.get("/api/usage/week").json()
    assert series[0]["start"].startswith("2026-11-25T19:00")
    assert sum(p["manual"] for p in series) == pytest.approx(1.0)


def test_resets_go_stale_after_an_hour(authed: TestClient, clock: FakeClock) -> None:
    csrf = authed.cookies[CSRF_COOKIE]
    authed.post("/api/probe", headers={CSRF_HEADER: csrf})
    clock.advance(timedelta(minutes=61))
    assert authed.get("/api/status").json()["resets_fresh"] is False


def test_spa_fallback_without_build(authed: TestClient) -> None:
    r = authed.get("/some/page")
    assert r.status_code in (200, 503)


def test_week_series_empty_before_any_reading(authed: TestClient) -> None:
    assert authed.get("/api/usage/week").json() == []
