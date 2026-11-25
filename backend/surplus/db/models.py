"""SQLAlchemy tables. Every schema change gets an Alembic migration; applied migrations are never edited."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC (SQLite has no tz support) and always returns aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to the database")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class UsageEventRow(Base):
    __tablename__ = "usage_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String, unique=True)  # ADR 0001: global dedup key
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    source: Mapped[str] = mapped_column(String)
    session_id: Mapped[str] = mapped_column(String, index=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str] = mapped_column(String)
    input_tok: Mapped[int] = mapped_column(Integer, default=0)
    output_tok: Mapped[int] = mapped_column(Integer, default=0)
    cache_create_tok: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tok: Mapped[int] = mapped_column(Integer, default=0)
    is_agent: Mapped[bool] = mapped_column(Boolean, default=False)
    is_sidechain: Mapped[bool] = mapped_column(Boolean, default=False)
    entrypoint: Mapped[str | None] = mapped_column(String, nullable=True)


class LimitReadingRow(Base):
    __tablename__ = "limit_reading"
    __table_args__ = (
        UniqueConstraint("bucket", "ts", "source", name="uq_limit_reading"),
        Index("ix_limit_reading_bucket_ts", "bucket", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    bucket: Mapped[str] = mapped_column(String)
    used_pct: Mapped[float] = mapped_column(Float)
    resets_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    source: Mapped[str] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String, nullable=True)


class SurfaceReadingRow(Base):
    """Weekly usage split by surface (claude_code / chat / …), from the probe. See ADR 0002."""

    __tablename__ = "surface_reading"
    __table_args__ = (UniqueConstraint("ts", "key", name="uq_surface_reading"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    key: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    pct_of_limit: Mapped[float] = mapped_column(Float)
    window_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class IngestCursorRow(Base):
    __tablename__ = "ingest_cursor"

    path: Mapped[str] = mapped_column(String, primary_key=True)
    inode: Mapped[int] = mapped_column(Integer)
    offset: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class SourceStatusRow(Base):
    """Health of each source, for the dashboard and `surplus doctor`."""

    __tablename__ = "source_status"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProjectRow(Base):
    """Index of a project folder. The .surplus/*.md files stay the authority for intent (plan §4.1)."""

    __tablename__ = "project"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    priority: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String)
    errors_json: Mapped[str] = mapped_column(Text, default="[]")
    indexed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    missing: Mapped[bool] = mapped_column(Boolean, default=False)
    registered: Mapped[bool] = mapped_column(
        Boolean, default=False
    )  # added explicitly, outside the projects root


class TaskRow(Base):
    """Mirror of backlog.md items, plus state that only the scheduler knows (later phases)."""

    __tablename__ = "task"
    __table_args__ = (UniqueConstraint("project_id", "backlog_id", name="uq_task_backlog"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("project.id", ondelete="CASCADE"), index=True)
    backlog_id: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String)  # open | done | proposed | blocked
    priority: Mapped[int] = mapped_column(Integer)
    est_size: Mapped[str | None] = mapped_column(String, nullable=True)
    position: Mapped[int] = mapped_column(Integer)


class SettingRow(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)


class RunRow(Base):
    """One agent run (plan §4.2, state machine in `runner/state.py`). Not a FK to `project`: history stays."""

    __tablename__ = "run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(String, index=True)
    exit_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str] = mapped_column(String)
    max_turns: Mapped[int] = mapped_column(Integer)
    budget_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    spent_pct_est: Mapped[float | None] = mapped_column(Float, nullable=True)
    branch: Mapped[str | None] = mapped_column(String, nullable=True)
    base_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    head_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    container: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    num_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    commits: Mapped[int] = mapped_column(Integer, default=0)
    verified_tests: Mapped[str | None] = mapped_column(String, nullable=True)  # pass|fail|none|error
    report_source: Mapped[str | None] = mapped_column(String, nullable=True)  # agent|repaired|fallback
    report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_path: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)  # merged|discarded|continued
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    # Limits this run was started with (project overrides or global defaults; ADR 0008).
    effort: Mapped[str | None] = mapped_column(String, nullable=True)
    cap_five_hour_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    cap_weekly_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    timeout_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A continuation resumes the parent's session on the parent's branch after the user allowed it.
    parent_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CostModelRow(Base):
    """One fitted coefficient of the cost model (plan §5.3): % of `bucket` per weighted token of `model`.

    All rows of one fit share `bucket`, `ts_fitted`, `weights_json`, `r2` and `low_confidence`.
    """

    __tablename__ = "cost_model"
    __table_args__ = (
        UniqueConstraint("bucket", "model", "ts_fitted", name="uq_cost_model"),
        Index("ix_cost_model_bucket_ts", "bucket", "ts_fitted"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)  # model family: haiku | sonnet | opus | other
    ts_fitted: Mapped[datetime] = mapped_column(UTCDateTime)
    pct_per_weighted_token: Mapped[float] = mapped_column(Float)
    weights_json: Mapped[str] = mapped_column(Text)
    r2: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String)  # fit | prior
    n_windows: Mapped[int] = mapped_column(Integer)  # windows of the whole fit
    model_windows: Mapped[int] = mapped_column(Integer)  # windows in which this family spent tokens
    low_confidence: Mapped[bool] = mapped_column(Boolean)
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
