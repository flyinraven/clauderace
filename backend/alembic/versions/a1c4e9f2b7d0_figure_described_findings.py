"""Text a station states when no image exists for a view.

A dynamic sign - fatiguable ptosis, Cogan's lid twitch, enhancement on
lifting the lid - has no still photograph. Without somewhere to say so, the
rubric marks the candidate on describing something they were never shown.

Revision ID: a1c4e9f2b7d0
Revises: 510501aa7893
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1c4e9f2b7d0"
down_revision = "510501aa7893"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("osce_figures", sa.Column("described_findings", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("osce_figures", "described_findings")
