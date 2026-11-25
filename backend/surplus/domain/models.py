"""Pydantic models at the boundaries. Internal currency is percent of a limit (0-100), never tokens."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from surplus.clock import ensure_utc


class UsageSourceKind(StrEnum):
    CC_LOG = "cc_log"
    AGENT = "agent"


class ReadingSource(StrEnum):
    PROBE = "probe"  # get_usage control request (ADR 0002)
    STREAM = "stream"  # rate_limit_event during a run
    STATUSLINE = "statusline"
    MANUAL = "manual"


# Known bucket ids. Bucket ids are open-ended strings (plan §2.4): model-scoped buckets arrive
# as "weekly_model:<name>", and new server buckets must not break ingestion.
FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"

BUCKET_PERIOD_HOURS: dict[str, int] = {FIVE_HOUR: 5}


def bucket_period_hours(bucket: str) -> int:
    """Length of a bucket's window. Everything that is not the 5-h session window is weekly."""
    return BUCKET_PERIOD_HOURS.get(bucket, 7 * 24)


class TokenCounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    input: int = 0
    output: int = 0
    cache_create: int = 0
    cache_read: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_create + self.cache_read


class UsageEvent(BaseModel):
    """One API response, as seen in a Claude Code log or an agent's stream."""

    model_config = ConfigDict(frozen=True)

    message_id: str
    ts: datetime
    source: UsageSourceKind
    session_id: str
    model: str
    tokens: TokenCounts
    is_agent: bool = False
    is_sidechain: bool = False
    entrypoint: str | None = None
    project_id: str | None = None
    run_id: str | None = None

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return ensure_utc(v)


class LimitReading(BaseModel):
    """An authoritative reading of one limit bucket."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    bucket: str
    used_pct: float = Field(ge=0, le=100)
    resets_at: datetime | None
    source: ReadingSource
    status: str | None = None  # allowed | allowed_warning | rejected (stream source only)

    @field_validator("ts", "resets_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return None if v is None else ensure_utc(v)


class SurfaceShare(BaseModel):
    """Weekly usage split by surface, from the probe's seven_day_breakdown (spike 2)."""

    model_config = ConfigDict(frozen=True)

    key: str  # claude_code | chat | cowork | other | …
    display_name: str
    pct_of_limit: float


class UsageSnapshot(BaseModel):
    """Everything one probe call returns that Surplus uses."""

    model_config = ConfigDict(frozen=True)

    ts: datetime
    subscription_type: str | None
    readings: list[LimitReading]
    weekly_breakdown: list[SurfaceShare] = Field(default_factory=list)
    weekly_window_started_at: datetime | None = None
