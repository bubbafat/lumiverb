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


def upgrade() -> None:
    op.add_column("assets", sa.Column("transcript_srt", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("transcript_text", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("transcript_language", sa.String(), nullable=True))
    op.add_column("assets", sa.Column("transcribed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("assets", "transcribed_at")
    op.drop_column("assets", "transcript_language")
    op.drop_column("assets", "transcript_text")
    op.drop_column("assets", "transcript_srt")
