"""What an ingested figure actually is, so a question can be given the right one.

Revision ID: e5f7b2c4d1a8
Revises: d4e6a1b9c2f3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5f7b2c4d1a8"
down_revision = "d4e6a1b9c2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("osce_figures", sa.Column("modality", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("osce_figures", "modality")
