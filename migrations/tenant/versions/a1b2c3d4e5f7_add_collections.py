"""Add collections and collection_assets tables.

Revision ID: a1b2c3d4e5f7
Revises: z3a4b5c6d7e8
Create Date: 2026-03-29

ADR-006: Collections — virtual groupings of assets across libraries.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = ("z3a4b5c6d7e8", "j9k0l1m2n3o4")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column("collection_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "cover_asset_id",
            sa.Text(),
            sa.ForeignKey("assets.asset_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("sort_order", sa.Text(), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "collection_assets",
        sa.Column("collection_id", sa.Text(), sa.ForeignKey("collections.collection_id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_id", sa.Text(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("collection_id", "asset_id"),
    )

    op.create_index("ix_collection_assets_asset_id", "collection_assets", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_collection_assets_asset_id", table_name="collection_assets")
    op.drop_table("collection_assets")
    op.drop_table("collections")
