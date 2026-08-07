"""A counter that retires every token issued before it was bumped.

Revision ID: f6a8c3d5e2b9
Revises: e5f7b2c4d1a8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f6a8c3d5e2b9"
down_revision = "e5f7b2c4d1a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
