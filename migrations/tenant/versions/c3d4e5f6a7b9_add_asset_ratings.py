"""Add asset_ratings table for user-scoped favorites, stars, and color labels.

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a8
Create Date: 2026-03-29

ADR-007: Ratings. Per-user rating table with composite PK (user_id, asset_id).
Supports favorites (boolean), stars (0-5), and color labels. ON DELETE CASCADE
on asset_id so hard-deleting an asset removes its ratings.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c3d4e5f6a7b9"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_ratings",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column(
            "asset_id",
            sa.Text(),
            sa.ForeignKey("assets.asset_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "favorite", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        sa.Column(
            "stars", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("color", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("user_id", "asset_id"),
        sa.CheckConstraint("stars >= 0 AND stars <= 5", name="ck_asset_ratings_stars"),
        sa.CheckConstraint(
            "color IS NULL OR color IN ('red', 'orange', 'yellow', 'green', 'blue', 'purple')",
            name="ck_asset_ratings_color",
        ),
    )
    # Partial indexes for fast filtered lookups
    op.create_index(
        "ix_asset_ratings_user_favorite",
        "asset_ratings",
        ["user_id"],
        postgresql_where=sa.text("favorite = TRUE"),
    )
    op.create_index(
        "ix_asset_ratings_user_stars",
        "asset_ratings",
        ["user_id", "stars"],
        postgresql_where=sa.text("stars > 0"),
    )
    op.create_index(
        "ix_asset_ratings_user_color",
        "asset_ratings",
        ["user_id"],
        postgresql_where=sa.text("color IS NOT NULL"),
    )
    # Index for cascade lookups when an asset is deleted
    op.create_index(
        "ix_asset_ratings_asset_id",
        "asset_ratings",
        ["asset_id"],
    )


def downgrade() -> None:
    op.drop_table("asset_ratings")
