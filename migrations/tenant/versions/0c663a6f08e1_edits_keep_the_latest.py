"""Edits keep only the latest (ADR-016, Robert Oct 9).

A person's description, tags, OCR text or transcript is the one copy: no
machine copy is kept underneath it, and machine output never replaces it.
A person's tag edit fixes the whole list (asset_corrections.tags, NULL: the
machine's), instead of adds and removes on the machine's list; the machine
transcripts kept under a person's go. No old data is kept (a fresh start
follows, Robert Oct 9): tag edits are dropped, not converted.

Revision ID: 0c663a6f08e1
Revises: 5fe827012184
Create Date: 2026-10-09
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0c663a6f08e1"
down_revision: Union[str, Sequence[str], None] = "5fe827012184"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("asset_corrections", sa.Column("tags", postgresql.JSONB(), nullable=True))
    op.drop_column("asset_corrections", "tags_added")
    op.drop_column("asset_corrections", "tags_removed")
    op.drop_table("machine_transcripts")


def downgrade() -> None:
    op.create_table(
        "machine_transcripts",
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("srt", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("lineage", postgresql.JSONB(), nullable=True),
        sa.Column("transcribed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("asset_corrections", sa.Column("tags_added", postgresql.JSONB(), nullable=False,
                                                 server_default=sa.text("'[]'::jsonb")))
    op.add_column("asset_corrections", sa.Column("tags_removed", postgresql.JSONB(), nullable=False,
                                                 server_default=sa.text("'[]'::jsonb")))
    op.drop_column("asset_corrections", "tags")
