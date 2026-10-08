"""Projects go to the trash before they're deleted for good.

Revision ID: u6v7w8x9y0z1
Revises: t5u6v7w8x9y0
Create Date: 2026-10-07

Same pattern as assets: deleting sets deleted_at, restore clears it, and
emptying the trash removes the row. Trash is separate from status, so a
restored project comes back active or archived, as it was.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "u6v7w8x9y0z1"
down_revision: Union[str, Sequence[str], None] = "t5u6v7w8x9y0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Projects in the trash would come back as live ones; delete them for
    # good instead, as emptying the trash would.
    op.execute(sa.text("DELETE FROM projects WHERE deleted_at IS NOT NULL"))
    op.drop_column("projects", "deleted_at")
