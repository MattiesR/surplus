"""Working-hours template (ADR 0015): parsing, values, the baseline using it, store, API and CLI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from surplus import preferences
from surplus.api.app import create_app
from surplus.api.auth import load_or_create_token
from surplus.clock import FakeClock
from surplus.collector.service import Collector
from surplus.config import Paths, Settings
from surplus.domain.events import EventBus
from surplus.domain.models import FIVE_HOUR, SEVEN_DAY
from surplus.predictor.baseline import SLOTS_PER_DAY, SlotBasis, fit_baseline, no_data_pct
from surplus.predictor.template import parse_working_hours, template_for

MON = 0
FRI = 4


def slot(day: int, hh: int, mm: int = 0) -> int:
    return day * SLOTS_PER_DAY + hh * 4 + mm // 15


def test_parse_weekdays() -> None:
    wh = parse_working_hours("Mon-Fri 09:00-18:00")
    assert len(wh.busy) == 5 * 9 * 4
    assert slot(MON, 9) in wh.busy and slot(FRI, 17, 45) in wh.busy
    assert slot(MON, 18) not in wh.busy and slot(5, 10) not in wh.busy and slot(MON, 8, 45) not in wh.busy
    assert "45 h a week" in wh.describe()


def test_parse_several_ranges_and_wrapping_days() -> None:
    wh = parse_working_hours("sat-mon 10-12; wed 9:30-10")
    assert {slot(5, 10), slot(6, 11, 45), slot(MON, 10), slot(2, 9, 30), slot(2, 9, 45)} <= wh.busy
    assert len(wh.busy) == 3 * 8 + 2


@pytest.mark.parametrize(
    "bad", ["", "weekdays 9-17", "mon-fri", "mon-fri 18-9", "mon-fri 9:10-17", "mon-fri 25-26", "xyz 9-17"]
)
def test_parse_rejects(bad: str) -> None:
    with pytest.raises(ValueError, match=r"empty|range|day|time|after|quarter"):
        parse_working_hours(bad)


def test_template_values() -> None:
    wh = parse_working_hours("mon-fri 09-18")
    five = template_for(wh, FIVE_HOUR)
    week = template_for(wh, SEVEN_DAY)
    assert five[slot(MON, 10)] == no_data_pct(FIVE_HOUR) and five[slot(MON, 20)] == 0
    assert sum(week) == pytest.approx(100.0)  # the whole weekly limit could be used in working hours
    assert week[slot(5, 12)] == 0


def test_baseline_without_history_frees_the_off_hours_only() -> None:
    wh = parse_working_hours("mon-fri 09-18")
    clock = FakeClock(datetime(2026, 11, 26, 12, tzinfo=UTC))
    model = fit_baseline(FIVE_HOUR, [], clock, UTC, template_for(wh, FIVE_HOUR))
    monday = datetime(2026, 11, 30, tzinfo=UTC)
    work = model.slot(monday + timedelta(hours=10))
    night = model.slot(monday + timedelta(hours=22))
    assert work.basis is SlotBasis.TEMPLATE and work.p90_pct == no_data_pct(FIVE_HOUR)
    assert night.basis is SlotBasis.TEMPLATE and night.p90_pct == 0


def test_store_and_collector_refit(sm: sessionmaker[Session], paths: Paths, clock: FakeClock) -> None:
    col = Collector(sm, paths, Settings(), clock, EventBus())
    before = col.forecast_model(FIVE_HOUR)
    assert col.working_hours() is None and before.basis[slot(MON, 22)] is SlotBasis.NO_DATA
    with sm.begin() as s:
        preferences.set_working_hours(s, "mon-fri 09-18")
    after = col.forecast_model(FIVE_HOUR)  # a changed template refits at once
    assert after is not before and after.p90[slot(MON, 22)] == 0
    with sm.begin() as s:
        assert preferences.set_working_hours(s, None) is None
    with sm() as s:
        assert preferences.get_working_hours(s) is None
    with sm.begin() as s, pytest.raises(ValueError, match="not a range"):
        preferences.set_working_hours(s, "nope")


def test_preferences_api(paths: Paths, clock: FakeClock) -> None:
    app = create_app(paths, Settings(), clock, background=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        h = {"Authorization": f"Bearer {load_or_create_token(paths.api_token_file)}"}
        assert c.get("/api/preferences", headers=h).json()["working_hours"] is None
        r = c.put("/api/preferences", json={"working_hours": "mon-fri 9-18"}, headers=h)
        assert r.status_code == 200 and r.json()["working_hours"] == "mon-fri 9-18"
        assert "45 h" in r.json()["working_hours_text"]
        assert c.put("/api/preferences", json={"working_hours": "always"}, headers=h).status_code == 422
        assert (
            c.put("/api/preferences", json={"working_hours": None}, headers=h).json()["working_hours"] is None
        )


def test_cli_hours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from surplus.cli import app

    for name in ("DATA", "CONFIG", "CLAUDE"):
        monkeypatch.setenv(f"SURPLUS_{name}_DIR", str(tmp_path / name))
    run = CliRunner()
    assert "No working hours" in run.invoke(app, ["hours"]).output
    assert "45 h a week" in run.invoke(app, ["hours", "mon-fri 09:00-18:00"]).output
    assert "mon-fri 09:00-18:00" in run.invoke(app, ["hours"]).output
    assert run.invoke(app, ["hours", "sometimes"]).exit_code != 0
    assert "removed" in run.invoke(app, ["hours", "none"]).output
