"""Add transcript columns to assets table.

Revision ID: k1l2m3n4o5p7
Revises: j0k1l2m3n4o6
Create Date: 2026-04-01

Adds storage for video transcripts: raw SRT content, plain text for search
indexing, detected language, and timestamp of when the transcript was added.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "k1l2m3n4o5p7"
down_revision: Union[str, Sequence[str], None] = "j0k1l2m3n4o6"
branch_labels = None
depends_on = None


_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    # Drop view — SELECT * caches columns; new columns won't appear without recreate
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))

    op.add_column("assets", sa.Column("transcript_srt", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("transcript_text", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("transcript_language", sa.String(), nullable=True))
    op.add_column("assets", sa.Column("transcribed_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("assets", "transcribed_at")
    op.drop_column("assets", "transcript_language")
    op.drop_column("assets", "transcript_text")
    op.drop_column("assets", "transcript_srt")
    op.execute(sa.text(_VIEW_DDL))
