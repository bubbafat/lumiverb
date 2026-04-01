"""Recreate active_assets view to include transcript columns.

Revision ID: l2m3n4o5p6q8
Revises: k1l2m3n4o5p7
Create Date: 2026-04-01

The previous migration added transcript columns to assets but didn't
recreate the active_assets view. PostgreSQL caches SELECT * column lists
at view creation time, so the view was missing the new columns.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "l2m3n4o5p6q8"
down_revision: Union[str, Sequence[str], None] = "k1l2m3n4o5p7"
branch_labels = None
depends_on = None

_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    pass  # View is the same either way
