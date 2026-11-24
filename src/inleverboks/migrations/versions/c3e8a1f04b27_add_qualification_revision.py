"""add qualification revision and previous crebo

Revision ID: c3e8a1f04b27
Revises: 517d1a5ff6a1
Create Date: 2026-11-24 10:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3e8a1f04b27"
down_revision: str | None = "517d1a5ff6a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("qualification", schema=None) as batch_op:
        batch_op.add_column(sa.Column("revision", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("previous_crebo", sa.String(length=10), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_qualification_previous_crebo"), ["previous_crebo"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("qualification", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_qualification_previous_crebo"))
        batch_op.drop_column("previous_crebo")
        batch_op.drop_column("revision")
