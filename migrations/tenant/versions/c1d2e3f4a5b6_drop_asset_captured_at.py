"""Drop dead captured_at column from assets table.

The captured_at column was never populated; taken_at is the canonical capture
timestamp. Remove it to keep the schema clean.

Revision ID: c1d2e3f4a5b6
Revises: b6c7d8e9f0a1
Create Date: 2026-03-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("assets", "captured_at")
    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.add_column(
        "assets",
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(sa.text(_VIEW_DDL))
