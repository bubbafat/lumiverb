"""Add saved_views table for bookmarked filter presets.

Revision ID: d4e5f6g7h8i9
Revises: c3d4e5f6a7b9
Create Date: 2026-03-29

ADR-008: Saved views. Named filter presets that navigate to /browse?{query_params}.
User-scoped, ordered by position.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d4e5f6g7h8i9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_views",
        sa.Column("view_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("query_params", sa.Text(), nullable=False),
        sa.Column("icon", sa.Text(), nullable=True),
        sa.Column("owner_user_id", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("view_id"),
    )
    op.create_index("ix_saved_views_owner_user_id", "saved_views", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_saved_views_owner_user_id")
    op.drop_table("saved_views")
