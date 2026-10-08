"""machine_transcripts: the machine's transcript, kept under a person's (ADR-016 phase 3, piece 4).

Revision ID: 96a9e82eebd7
Revises: b799bec833a5
Create Date: 2026-10-08

A clip shows one transcript (assets.transcript_*). A person's replaces the
machine's on screen but not in storage: the latest machine transcript is
kept here, with how it was made, so removing the person's brings it back
and re-transcribing never touches theirs. Backfilled from the machine
transcripts clips show now.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "96a9e82eebd7"
down_revision: Union[str, Sequence[str], None] = "b799bec833a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "machine_transcripts",
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("srt", sa.Text(), nullable=True),  # NULL: no speech
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("lineage", postgresql.JSONB(), nullable=True),
        sa.Column("transcribed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(sa.text(
        "INSERT INTO machine_transcripts (asset_id, srt, text, language, source, lineage, transcribed_at)"
        " SELECT a.asset_id, a.transcript_srt, a.transcript_text, a.transcript_language,"
        "   COALESCE(a.transcript_source, 'unknown'),"
        "   (SELECT jsonb_build_object('producer', l.producer, 'version', l.producer_version,"
        "                              'settings_hash', l.settings_hash, 'source_sha256', l.source_sha256)"
        "    FROM artifact_lineage l WHERE l.asset_id = a.asset_id AND l.artifact = 'transcript'"
        "      AND l.producer NOT IN ('', 'person')),"
        "   COALESCE(a.transcribed_at, now())"
        " FROM assets a"
        # Transcripts from before transcript_source existed have none: the machine's too.
        " WHERE COALESCE(a.transcript_source, '') <> 'manual'"
        "   AND (a.transcript_srt IS NOT NULL OR a.transcribed_at IS NOT NULL"
        "        OR (a.transcript_source IS NOT NULL AND a.has_transcript IS NOT NULL))"
    ))


def downgrade() -> None:
    op.drop_table("machine_transcripts")
