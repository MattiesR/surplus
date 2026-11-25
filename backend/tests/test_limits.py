"""Contract tests against formats recorded on 2026-11-25 (spike 2). If the CLI changes them, these fail first."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from surplus.collector.limits import (
    LimitFormatError,
    parse_get_usage,
    parse_rate_limit_event,
    parse_statusline,
)
from surplus.domain.models import ReadingSource

from conftest import FIXTURES, T0, load_fixture


def _probe_payload() -> dict[str, object]:
    payload: dict[str, object] = load_fixture("probe/get_usage_response.json")["response"]["response"]
    return payload


def test_get_usage_fixture() -> None:
    snap = parse_get_usage(_probe_payload(), T0)
    by_bucket = {r.bucket: r for r in snap.readings}
    assert set(by_bucket) == {"five_hour", "seven_day"}  # null buckets are skipped
    five = by_bucket["five_hour"]
    assert five.used_pct == 6
    assert five.resets_at == datetime(2026, 11, 26, 0, 30, tzinfo=UTC)  # µs noise rounded away
    assert by_bucket["seven_day"].resets_at == datetime(2026, 12, 2, 19, 0, tzinfo=UTC)
    assert all(r.source is ReadingSource.PROBE for r in snap.readings)
    assert snap.subscription_type == "pro"
    assert {s.key for s in snap.weekly_breakdown} == {"claude_code", "chat", "cowork", "other"}
    assert snap.weekly_window_started_at == datetime(2026, 11, 25, 19, 0, tzinfo=UTC)


def test_get_usage_model_scoped_bucket() -> None:
    payload = _probe_payload()
    rl = dict(payload["rate_limits"])  # type: ignore[call-overload]
    rl["model_scoped"] = [{"display_name": "Fable", "utilization": 40, "resets_at": "2026-12-02T19:00:00Z"}]
    rl["seven_day_sonnet"] = {"utilization": 12.5, "resets_at": "2026-12-02T19:00:00+00:00"}
    snap = parse_get_usage({**payload, "rate_limits": rl}, T0)
    buckets = {r.bucket: r.used_pct for r in snap.readings}
    assert buckets["weekly_model:fable"] == 40
    assert buckets["seven_day_sonnet"] == 12.5


def test_get_usage_unavailable_is_empty_not_error() -> None:
    snap = parse_get_usage({"rate_limits_available": False, "rate_limits": None}, T0)
    assert snap.readings == []


def test_get_usage_garbage_raises() -> None:
    with pytest.raises(LimitFormatError):
        parse_get_usage({"something": "else"}, T0)


def test_get_usage_clamps_and_tolerates_unknown_keys() -> None:
    payload = {
        "rate_limits": {
            "five_hour": {"utilization": 130, "resets_at": None, "new_field": 1},
            "brand_new_bucket": {"utilization": 3, "resets_at": 1796238000},
            "weird": "string",
        }
    }
    readings = {r.bucket: r for r in parse_get_usage(payload, T0).readings}
    assert readings["five_hour"].used_pct == 100
    assert readings["five_hour"].resets_at is None
    assert readings["brand_new_bucket"].resets_at == datetime(2026, 12, 2, 19, 0, tzinfo=UTC)


def test_rate_limit_event_fixture_is_fraction() -> None:
    lines = (FIXTURES / "probe/run_stream.jsonl").read_text().splitlines()
    event = next(json.loads(line) for line in lines if '"rate_limit_event"' in line)
    readings = {r.bucket: r for r in parse_rate_limit_event(event, T0)}
    assert readings["five_hour"].used_pct == pytest.approx(6.0)
    assert readings["seven_day"].used_pct == pytest.approx(1.0)
    assert readings["five_hour"].resets_at == datetime(2026, 11, 26, 0, 30, tzinfo=UTC)
    assert readings["five_hour"].status == "allowed"
    assert readings["five_hour"].source is ReadingSource.STREAM


def test_rate_limit_event_legacy_shape() -> None:
    event = {
        "rate_limit_info": {
            "status": "rejected",
            "rateLimitType": "five_hour",
            "utilization": 1.0,
            "resetsAt": 1795653000,
        }
    }
    (r,) = parse_rate_limit_event(event, T0)
    assert r.used_pct == 100 and r.status == "rejected"


def test_statusline_payload() -> None:
    payload = {
        "cwd": "/somewhere",
        "rate_limits": {
            "five_hour": {"used_percentage": 23.5, "resets_at": 1795653000},
            "seven_day": {"used_percentage": 41, "resets_at": 1796238000},
            "spend_limit": {"used_percentage": 5, "resets_at": 1796238000},
        },
    }
    readings = {r.bucket: r for r in parse_statusline(payload, T0)}
    assert set(readings) == {"five_hour", "seven_day"}
    assert readings["five_hour"].used_pct == 23.5
    assert parse_statusline({"model": {}}, T0) == []
