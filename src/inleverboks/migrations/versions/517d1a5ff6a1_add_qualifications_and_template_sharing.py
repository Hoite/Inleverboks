"""add qualifications and template sharing

Revision ID: 517d1a5ff6a1
Revises: 75935fec392f
Create Date: 2026-11-23 22:10:13.481198
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "517d1a5ff6a1"
down_revision: str | None = "75935fec392f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "qualification",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("crebo", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("level", sa.Integer(), nullable=True),
        sa.Column("dossier_crebo", sa.String(length=10), nullable=True),
        sa.Column("dossier_name", sa.String(length=300), nullable=True),
        sa.Column("dossier_version", sa.String(length=40), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_qualification")),
        sa.UniqueConstraint("crebo", "dossier_version", name=op.f("uq_qualification_crebo")),
    )
    with op.batch_alter_table("qualification", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_qualification_crebo"), ["crebo"], unique=False)

    op.create_table(
        "core_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("qualification_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["qualification_id"],
            ["qualification.id"],
            name=op.f("fk_core_task_qualification_id_qualification"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_core_task")),
        sa.UniqueConstraint("qualification_id", "code", name=op.f("uq_core_task_qualification_id")),
    )
    with op.batch_alter_table("core_task", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_core_task_qualification_id"), ["qualification_id"], unique=False
        )

    op.create_table(
        "work_process",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("core_task_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["core_task_id"],
            ["core_task.id"],
            name=op.f("fk_work_process_core_task_id_core_task"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_process")),
        sa.UniqueConstraint("core_task_id", "code", name=op.f("uq_work_process_core_task_id")),
    )
    with op.batch_alter_table("work_process", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_work_process_core_task_id"), ["core_task_id"], unique=False
        )

    with op.batch_alter_table("exam", schema=None) as batch_op:
        batch_op.add_column(sa.Column("qualification", sa.JSON(), nullable=True))

    with op.batch_alter_table("exam_template", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("shared", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(sa.Column("qualification_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("core_task_code", sa.String(length=20), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_exam_template_qualification_id"), ["qualification_id"], unique=False
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_exam_template_qualification_id_qualification"),
            "qualification",
            ["qualification_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("exam_template_slot", schema=None) as batch_op:
        batch_op.add_column(sa.Column("work_process_code", sa.String(length=30), nullable=True))

    with op.batch_alter_table("expected_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("work_process_code", sa.String(length=30), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("expected_file", schema=None) as batch_op:
        batch_op.drop_column("work_process_code")

    with op.batch_alter_table("exam_template_slot", schema=None) as batch_op:
        batch_op.drop_column("work_process_code")

    with op.batch_alter_table("exam_template", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_exam_template_qualification_id_qualification"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_exam_template_qualification_id"))
        batch_op.drop_column("core_task_code")
        batch_op.drop_column("qualification_id")
        batch_op.drop_column("shared")

    with op.batch_alter_table("exam", schema=None) as batch_op:
        batch_op.drop_column("qualification")

    with op.batch_alter_table("work_process", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_work_process_core_task_id"))

    op.drop_table("work_process")
    with op.batch_alter_table("core_task", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_core_task_qualification_id"))

    op.drop_table("core_task")
    with op.batch_alter_table("qualification", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_qualification_crebo"))

    op.drop_table("qualification")
