"""Add owner_user_id and visibility to collections.

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-03-29

ADR-006 Phase 3.5: User-scoped collections. Collections are owned by a user
and private by default. Visibility can be 'private', 'shared' (all tenant
users), or 'public' (unauthenticated). Replaces the is_public boolean.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b2c3d4e5f6a8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("owner_user_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "collections",
        sa.Column(
            "visibility",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'private'"),
        ),
    )
    # Backfill: existing collections with is_public=true become 'shared',
    # others become 'private'. owner_user_id stays NULL for legacy collections
    # (treated as tenant-wide / shared).
    op.execute(
        sa.text(
            "UPDATE collections SET visibility = 'shared' WHERE is_public = TRUE"
        )
    )
    op.drop_column("collections", "is_public")


def downgrade() -> None:
    op.add_column(
        "collections",
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE collections SET is_public = TRUE WHERE visibility IN ('shared', 'public')"
        )
    )
    op.drop_column("collections", "visibility")
    op.drop_column("collections", "owner_user_id")
