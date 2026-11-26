"""run mode (work or split)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-26 14:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("run") as batch:
        batch.add_column(sa.Column("mode", sa.String(), nullable=False, server_default="work"))


def downgrade() -> None:
    with op.batch_alter_table("run") as batch:
        batch.drop_column("mode")
