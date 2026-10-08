"""asset_corrections: a person's description, OCR and tag edits (ADR-016 phase 3, piece 4).

Revision ID: fab90a220ba5
Revises: 96a9e82eebd7
Create Date: 2026-10-08

Beside the machine's values, never over them: a corrected description or
OCR wins while the machine's goes on being made underneath; tag edits are
adds and removes on top of the machine's list, so a new list keeps them.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "fab90a220ba5"
down_revision: Union[str, Sequence[str], None] = "96a9e82eebd7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_corrections",
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("description", sa.Text(), nullable=True),  # NULL: the machine's
        sa.Column("ocr_text", sa.Text(), nullable=True),  # NULL: the machine's
        sa.Column("tags_added", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("tags_removed", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("asset_corrections")
