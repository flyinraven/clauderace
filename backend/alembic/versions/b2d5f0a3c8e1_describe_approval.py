"""Descriptions are held for review before a candidate sees them.

Revision ID: b2d5f0a3c8e1
Revises: a1c4e9f2b7d0
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b2d5f0a3c8e1"
down_revision = "a1c4e9f2b7d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "osce_figures",
        sa.Column(
            "described_findings_approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("osce_figures", "described_findings_approved")
