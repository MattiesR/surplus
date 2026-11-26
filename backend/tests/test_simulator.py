"""The simulator (plan §13, Phase 4 DoD): 0 user-blocked incidents across 3 profiles x 4 weeks, and at least
75% utilisation for the light user. "CI fails if a known profile produces any incident."
"""

from __future__ import annotations

import random
from datetime import datetime

import pytest

from surplus.optimizer.budget import usage_trend
from surplus.simulator.engine import WEEKLY_RESET, SimConfig, simulate
from surplus.simulator.profiles import PROFILES, SLOT, Profile


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_profile_has_no_user_blocked_incidents(name: str) -> None:
    res = simulate(PROFILES[name], SimConfig(weeks=4))
    assert len(res.weeks) == 4
    assert res.incidents == [], res.summary()
    assert res.runs > 0  # agents did work: an idle optimizer would pass trivially
    if name == "light":
        assert res.utilisation >= 0.75, res.summary()


def test_more_seeds_stay_clean() -> None:
    for name, profile in PROFILES.items():
        for seed in (2, 3):
            res = simulate(profile, SimConfig(weeks=2, seed=seed))
            assert res.incidents == [], f"{name} seed {seed}: {res.summary()}"


def test_sudden_change_of_habits_is_caught_by_the_weekly_trend() -> None:
    """Three light weeks, then a heavy one the forecast has never seen (ADR 0012, trend correction)."""
    light, heavy = PROFILES["light"], PROFILES["heavy-weekday"]

    def switch(start: datetime, n: int, rng: random.Random) -> list[float]:
        cut = int((WEEKLY_RESET - start) / SLOT)
        a = light.generate(start, n, random.Random(1))
        b = heavy.generate(start, n, random.Random(2))
        return a[:cut] + b[cut:]

    res = simulate(Profile("light-then-heavy", "", switch), SimConfig(weeks=2))
    assert res.incidents == [], res.summary()


def test_agents_never_run_while_the_user_works() -> None:
    res = simulate(PROFILES["heavy-weekday"], SimConfig(weeks=1))
    assert res.kill_switch_stops > 0  # the user came back during runs, and the runs stopped


def test_usage_trend() -> None:
    assert usage_trend(0, 0) == 1
    assert usage_trend(5, 10) == 1  # quieter than forecast: never lowers the forecast
    assert usage_trend(28, 2) == pytest.approx(30 / 4)
    assert usage_trend(1, 0) == pytest.approx(1.5)  # smoothing keeps the first hours calm


def test_cli_simulate_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from surplus.cli import app

    out = CliRunner().invoke(app, ["simulate", "--profile", "light", "--weeks", "1"])
    assert out.exit_code == 0, out.output
    assert "0 user-blocked incidents" in out.output and "week of" in out.output
    bad = CliRunner().invoke(app, ["simulate", "--profile", "nope"])
    assert bad.exit_code != 0


@pytest.mark.parametrize("name", ["light", "irregular"])
def test_cold_start_with_a_working_hours_template_stays_safe(name: str) -> None:
    """No history at all, only the template; for the irregular user it is wrong (they work evenings)."""
    res = simulate(PROFILES[name], SimConfig(weeks=2, warmup_weeks=0, working_hours="mon-fri 09-18"))
    assert res.incidents == [], res.summary()
    assert res.runs > 0
