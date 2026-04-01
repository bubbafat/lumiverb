"""Add note columns to assets table.

Revision ID: m3n4o5p6q7r9
Revises: l2m3n4o5p6q8
Create Date: 2026-04-01

Adds freeform note, author tracking, and timestamp per asset.
One note per asset, editable by admin/editor, no history.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "m3n4o5p6q7r9"
down_revision: Union[str, Sequence[str], None] = "l2m3n4o5p6q8"
branch_labels = None
depends_on = None

_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.add_column("assets", sa.Column("note", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("note_author", sa.String(), nullable=True))
    op.add_column("assets", sa.Column("note_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("assets", "note_updated_at")
    op.drop_column("assets", "note_author")
    op.drop_column("assets", "note")
    op.execute(sa.text(_VIEW_DDL))
