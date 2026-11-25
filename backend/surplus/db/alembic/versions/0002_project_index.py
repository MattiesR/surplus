"""project index

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-25 21:39:44.063504
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("errors_json", sa.Text(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(), nullable=False),
        sa.Column("missing", sa.Boolean(), nullable=False),
        sa.Column("registered", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path"),
    )
    op.create_table(
        "task",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("backlog_id", sa.String(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("est_size", sa.String(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "backlog_id", name="uq_task_backlog"),
    )
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_task_project_id"), ["project_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("task", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_task_project_id"))

    op.drop_table("task")
    op.drop_table("project")
