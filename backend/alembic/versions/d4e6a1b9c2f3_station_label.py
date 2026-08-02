"""A station's printed name, where a number alone does not identify it.

2022 Semester 2 runs 1A, 1B, 2A ... 9B: eighteen stations sharing nine numbers.

Revision ID: d4e6a1b9c2f3
Revises: b2d5f0a3c8e1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e6a1b9c2f3"
down_revision = "b2d5f0a3c8e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "osce_stations",
        sa.Column("station_label", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("osce_stations", "station_label")
