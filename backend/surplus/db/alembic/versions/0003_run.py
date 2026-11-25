"""run table

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-25 22:40:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=True),
        sa.Column("task_title", sa.Text(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("exit_reason", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("max_turns", sa.Integer(), nullable=False),
        sa.Column("budget_pct", sa.Float(), nullable=True),
        sa.Column("spent_pct_est", sa.Float(), nullable=True),
        sa.Column("branch", sa.String(), nullable=True),
        sa.Column("base_branch", sa.String(), nullable=True),
        sa.Column("base_sha", sa.String(), nullable=True),
        sa.Column("head_sha", sa.String(), nullable=True),
        sa.Column("container", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("num_turns", sa.Integer(), nullable=True),
        sa.Column("commits", sa.Integer(), nullable=False),
        sa.Column("verified_tests", sa.String(), nullable=True),
        sa.Column("report_source", sa.String(), nullable=True),
        sa.Column("report_json", sa.Text(), nullable=True),
        sa.Column("report_path", sa.String(), nullable=True),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("base_dirty", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_run_project_id", "run", ["project_id"])
    op.create_index("ix_run_state", "run", ["state"])


def downgrade() -> None:
    op.drop_index("ix_run_state", table_name="run")
    op.drop_index("ix_run_project_id", table_name="run")
    op.drop_table("run")
