"""Add cover_asset_id to libraries.

Revision ID: q2r3s4t5u6v7
Revises: p1q2r3s4t5u6
Create Date: 2026-04-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "q2r3s4t5u6v7"
down_revision: Union[str, Sequence[str], None] = "p1q2r3s4t5u6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column("cover_asset_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_libraries_cover_asset_id",
        "libraries",
        "assets",
        ["cover_asset_id"],
        ["asset_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_libraries_cover_asset_id", "libraries", type_="foreignkey")
    op.drop_column("libraries", "cover_asset_id")
