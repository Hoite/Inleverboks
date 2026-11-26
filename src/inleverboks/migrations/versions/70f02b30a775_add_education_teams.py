"""add education teams

Revision ID: 70f02b30a775
Revises: d4f2b7a91c35
Create Date: 2026-11-26 16:08:38.420880
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "70f02b30a775"
down_revision: str | None = "d4f2b7a91c35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# PLAN B69: existing class lists, templates and exams keep team_id NULL; an admin assigns
# them afterwards. Nothing is guessed here.
_TEAM_TABLES = ("class_list", "exam", "exam_template")


def upgrade() -> None:
    op.create_table(
        "team",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_team")),
        sa.UniqueConstraint("name", name=op.f("uq_team_name")),
    )
    op.create_table(
        "team_crebo",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("crebo", sa.String(length=10), nullable=False),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.id"], name=op.f("fk_team_crebo_team_id_team"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("team_id", "crebo", name=op.f("pk_team_crebo")),
    )
    op.create_index(op.f("ix_team_crebo_crebo"), "team_crebo", ["crebo"], unique=False)
    op.create_table(
        "team_member",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("is_contact", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("added_by_id", sa.Uuid(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["added_by_id"],
            ["app_user.id"],
            name=op.f("fk_team_member_added_by_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.id"], name=op.f("fk_team_member_team_id_team"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["app_user.id"],
            name=op.f("fk_team_member_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("team_id", "user_id", name=op.f("pk_team_member")),
    )
    op.create_index(op.f("ix_team_member_user_id"), "team_member", ["user_id"], unique=False)

    for table in _TEAM_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("team_id", sa.Uuid(), nullable=True))
            batch_op.create_index(batch_op.f(f"ix_{table}_team_id"), ["team_id"], unique=False)
            batch_op.create_foreign_key(
                batch_op.f(f"fk_{table}_team_id_team"),
                "team",
                ["team_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            if table == "exam_template":
                batch_op.create_check_constraint(
                    "shared_or_team", "NOT (shared AND team_id IS NOT NULL)"
                )


def downgrade() -> None:
    for table in reversed(_TEAM_TABLES):
        with op.batch_alter_table(table, schema=None) as batch_op:
            if table == "exam_template":
                batch_op.drop_constraint("shared_or_team", type_="check")
            batch_op.drop_constraint(batch_op.f(f"fk_{table}_team_id_team"), type_="foreignkey")
            batch_op.drop_index(batch_op.f(f"ix_{table}_team_id"))
            batch_op.drop_column("team_id")
    op.drop_index(op.f("ix_team_member_user_id"), table_name="team_member")
    op.drop_table("team_member")
    op.drop_index(op.f("ix_team_crebo_crebo"), table_name="team_crebo")
    op.drop_table("team_crebo")
    op.drop_table("team")
