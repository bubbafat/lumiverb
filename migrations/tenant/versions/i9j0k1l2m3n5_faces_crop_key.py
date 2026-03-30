"""Add crop_key to faces table for face crop thumbnails.

Revision ID: i9j0k1l2m3n5
Revises: h8i9j0k1l2m4
Create Date: 2026-03-30
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "i9j0k1l2m3n5"
down_revision: Union[str, Sequence[str], None] = "h8i9j0k1l2m4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("faces", sa.Column("crop_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("faces", "crop_key")
