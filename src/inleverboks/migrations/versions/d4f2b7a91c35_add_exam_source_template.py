"""add the source template of an exam

Revision ID: d4f2b7a91c35
Revises: c3e8a1f04b27
Create Date: 2026-11-25 13:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f2b7a91c35"
down_revision: str | None = "c3e8a1f04b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing exams keep NULL: their expected files stay editable as before.
    with op.batch_alter_table("exam", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_template_id", sa.Uuid(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_exam_source_template_id"), ["source_template_id"], unique=False
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_exam_source_template_id_exam_template"),
            "exam_template",
            ["source_template_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("exam", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_exam_source_template_id_exam_template"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_exam_source_template_id"))
        batch_op.drop_column("source_template_id")
