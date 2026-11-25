"""initial collector schema

Revision ID: 0001
Revises:
Create Date: 2026-11-25 21:10:08.931022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingest_cursor",
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("inode", sa.Integer(), nullable=False),
        sa.Column("offset", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("path"),
    )
    op.create_table(
        "limit_reading",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("bucket", sa.String(), nullable=False),
        sa.Column("used_pct", sa.Float(), nullable=False),
        sa.Column("resets_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bucket", "ts", "source", name="uq_limit_reading"),
    )
    with op.batch_alter_table("limit_reading", schema=None) as batch_op:
        batch_op.create_index("ix_limit_reading_bucket_ts", ["bucket", "ts"], unique=False)

    op.create_table(
        "setting",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "source_status",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("last_ok_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "surface_reading",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("pct_of_limit", sa.Float(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ts", "key", name="uq_surface_reading"),
    )
    with op.batch_alter_table("surface_reading", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_surface_reading_ts"), ["ts"], unique=False)

    op.create_table(
        "usage_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("input_tok", sa.Integer(), nullable=False),
        sa.Column("output_tok", sa.Integer(), nullable=False),
        sa.Column("cache_create_tok", sa.Integer(), nullable=False),
        sa.Column("cache_read_tok", sa.Integer(), nullable=False),
        sa.Column("is_agent", sa.Boolean(), nullable=False),
        sa.Column("is_sidechain", sa.Boolean(), nullable=False),
        sa.Column("entrypoint", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"),
    )
    with op.batch_alter_table("usage_event", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_usage_event_session_id"), ["session_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_usage_event_ts"), ["ts"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("usage_event", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_usage_event_ts"))
        batch_op.drop_index(batch_op.f("ix_usage_event_session_id"))

    op.drop_table("usage_event")
    with op.batch_alter_table("surface_reading", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_surface_reading_ts"))

    op.drop_table("surface_reading")
    op.drop_table("source_status")
    op.drop_table("setting")
    with op.batch_alter_table("limit_reading", schema=None) as batch_op:
        batch_op.drop_index("ix_limit_reading_bucket_ts")

    op.drop_table("limit_reading")
    op.drop_table("ingest_cursor")
