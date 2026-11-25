"""run limits and continuations

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25 23:10:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("run") as batch:
        batch.add_column(sa.Column("effort", sa.String(), nullable=True))
        batch.add_column(sa.Column("cap_five_hour_pct", sa.Float(), nullable=True))
        batch.add_column(sa.Column("cap_weekly_pct", sa.Float(), nullable=True))
        batch.add_column(sa.Column("timeout_minutes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("parent_run_id", sa.Integer(), nullable=True))
        batch.create_index("ix_run_parent_run_id", ["parent_run_id"])


def downgrade() -> None:
    with op.batch_alter_table("run") as batch:
        batch.drop_index("ix_run_parent_run_id")
        batch.drop_column("parent_run_id")
        batch.drop_column("timeout_minutes")
        batch.drop_column("cap_weekly_pct")
        batch.drop_column("cap_five_hour_pct")
        batch.drop_column("effort")
