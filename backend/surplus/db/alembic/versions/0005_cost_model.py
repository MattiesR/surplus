"""cost model

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-25 23:50:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cost_model",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("bucket", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("ts_fitted", sa.DateTime(), nullable=False),
        sa.Column("pct_per_weighted_token", sa.Float(), nullable=False),
        sa.Column("weights_json", sa.Text(), nullable=False),
        sa.Column("r2", sa.Float(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("n_windows", sa.Integer(), nullable=False),
        sa.Column("model_windows", sa.Integer(), nullable=False),
        sa.Column("low_confidence", sa.Boolean(), nullable=False),
        sa.Column("reasons_json", sa.Text(), nullable=False, server_default="[]"),
        sa.UniqueConstraint("bucket", "model", "ts_fitted", name="uq_cost_model"),
    )
    op.create_index("ix_cost_model_bucket_ts", "cost_model", ["bucket", "ts_fitted"])


def downgrade() -> None:
    op.drop_index("ix_cost_model_bucket_ts", table_name="cost_model")
    op.drop_table("cost_model")
