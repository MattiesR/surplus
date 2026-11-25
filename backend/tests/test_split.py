from __future__ import annotations

from datetime import timedelta

import pytest

from surplus.collector.split import (
    SurfacePoint,
    WeightedEvent,
    hourly_series,
    model_multiplier,
    weekly_split,
    weighted_tokens,
)
from surplus.domain.models import SurfaceShare, TokenCounts

from conftest import T0


def shares(cc: float, chat: float, other: float = 0) -> list[SurfaceShare]:
    return [
        SurfaceShare(key="claude_code", display_name="Claude Code", pct_of_limit=cc),
        SurfaceShare(key="chat", display_name="Chats", pct_of_limit=chat),
        SurfaceShare(key="other", display_name="Other", pct_of_limit=other),
    ]


def test_weighted_tokens_prior() -> None:
    t = TokenCounts(input=10, output=2, cache_create=4, cache_read=100)
    assert weighted_tokens("claude-haiku-5-5", t) == pytest.approx(10 + 10 + 5 + 10)
    assert weighted_tokens("claude-opus-5-5", t) == pytest.approx(5 * 35)


def test_unknown_model_is_priced_conservatively() -> None:
    assert model_multiplier("claude-fable-5-2") == model_multiplier("claude-opus-5-5")


def test_weekly_split() -> None:
    s = weekly_split(shares(cc=20, chat=5, other=1), manual_wt=300, agent_wt=100)
    assert s is not None
    assert s.manual == pytest.approx(15)
    assert s.agent == pytest.approx(5)
    assert s.invisible == pytest.approx(6)
    assert s.total == pytest.approx(26)


def test_weekly_split_without_breakdown() -> None:
    assert weekly_split([], 1, 1) is None


def test_weekly_split_no_tokens_means_all_manual() -> None:
    s = weekly_split(shares(cc=4, chat=0), 0, 0)
    assert s is not None and s.agent == 0 and s.manual == 4


def test_hourly_series_scales_visible_and_diffs_invisible() -> None:
    events = [
        WeightedEvent(T0 + timedelta(minutes=10), 100, False),
        WeightedEvent(T0 + timedelta(hours=1, minutes=5), 300, True),
        WeightedEvent(T0 - timedelta(hours=1), 999, False),  # outside the range: ignored
    ]
    surfaces = [
        SurfacePoint(T0, 0),
        SurfacePoint(T0 + timedelta(hours=1, minutes=30), 2.0),
        SurfacePoint(T0 + timedelta(hours=2, minutes=30), 1.5),  # a drop (reset) never goes negative
    ]
    pts = hourly_series(events, surfaces, visible_pct=8.0, start=T0, end=T0 + timedelta(hours=3))
    assert len(pts) == 3
    assert pts[0].manual == pytest.approx(2.0)
    assert pts[1].agent == pytest.approx(6.0)
    assert pts[1].invisible == pytest.approx(2.0)
    assert pts[2].invisible == 0
    assert sum(p.manual + p.agent for p in pts) == pytest.approx(8.0)
