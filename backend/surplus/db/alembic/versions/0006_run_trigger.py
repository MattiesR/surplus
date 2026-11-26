"""run trigger (manual or auto)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-26 10:30:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("run") as batch:
        batch.add_column(sa.Column("trigger", sa.String(), nullable=False, server_default="manual"))


def downgrade() -> None:
    with op.batch_alter_table("run") as batch:
        batch.drop_column("trigger")
