"""Transcripts: assets.transcript_synced_at, so the search sweep puts them back.

Revision ID: e8a1b2c3d4f5
Revises: d7f2a3b4c5e6
Create Date: 2026-10-09

The transcript index was only filled when a transcript was submitted, so a
lost one stayed empty. Like search_synced_at for clips and scenes, it says
when the clip's transcript segments last went into search; the sweep indexes
every transcript that's newer, or never went in. Null for every clip at
first: the sweep indexes each transcript once.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e8a1b2c3d4f5"
down_revision: Union[str, Sequence[str], None] = "d7f2a3b4c5e6"
branch_labels = None
depends_on = None

_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.add_column("assets", sa.Column("transcript_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("assets", "transcript_synced_at")
    op.execute(sa.text(_VIEW_DDL))
