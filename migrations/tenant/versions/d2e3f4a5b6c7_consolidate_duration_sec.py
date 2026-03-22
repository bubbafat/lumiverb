"""Consolidate duration_sec from duration_ms and drop duration_ms column.

Backfill duration_sec where NULL from duration_ms, then drop duration_ms.
After this migration, duration_sec is the single canonical duration field.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-03-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    # Backfill duration_sec from duration_ms where still NULL
    op.execute(
        sa.text(
            "UPDATE assets SET duration_sec = duration_ms / 1000.0 "
            "WHERE duration_ms IS NOT NULL AND duration_sec IS NULL"
        )
    )
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("assets", "duration_ms")
    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.add_column(
        "assets",
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
    )
    # Restore duration_ms from duration_sec
    op.execute(
        sa.text(
            "UPDATE assets SET duration_ms = (duration_sec * 1000)::bigint "
            "WHERE duration_sec IS NOT NULL"
        )
    )
    op.execute(sa.text(_VIEW_DDL))
