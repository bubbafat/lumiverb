"""Text search in PostgreSQL (ADR-019): each clip's, scene's and transcript segment's words, indexed.

Revision ID: d5f9b3a7c2e1
Revises: c4e8a2f6b1d9
Create Date: 2026-10-15

- asset_search: a clip's searchable text as a person sees it (its description
  and tags, a person's corrections over them, the text read in it, its note,
  its transcript and its path's words), kept by the search sync
  (src/server/search/sync.py), and its words computed from it: everything (an
  account's own search), shown (a public page: no note, no path) and
  shown_capped (a public page that plays only the start of a video: no
  transcript either). Each has its GIN index; descriptions, tags and notes
  weigh most, then the text read and the transcript, then the path.
- video_scenes.words: a scene's description and tags, computed with the row:
  nothing to keep in step, so its search_synced_at goes.
- transcript_segments: a transcript's segments, each with its times and words,
  written with the transcript.
- Every clip's and transcript's sync time is cleared: the sweep fills the new
  tables from what the database holds (no AI runs again). What the Quickwit
  sync kept in system_metadata goes (search.*: its indexes, rebuilds,
  reindex and remake marks, fallback and failure notes).

Down: drops them all; the sync times are cleared again, so the search sync
fills whatever it then writes to.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f9b3a7c2e1"
down_revision: str | Sequence[str] | None = "c4e8a2f6b1d9"
branch_labels = None
depends_on = None


def _words(*parts: tuple[str, str, str]) -> str:
    """A tsvector of text columns, each (column, text search config, weight)."""
    return " || ".join(f"setweight(to_tsvector('{config}', {column}), '{weight}')" for column, config, weight in parts)


DESCRIBED = ("described", "english", "A")
NOTE = ("note", "english", "A")
OCR = ("ocr_text", "english", "C")
TRANSCRIPT = ("transcript_text", "english", "C")
PATH = ("path_words", "english", "D")  # as typed words are read: "family" must find a folder "Family"


def upgrade() -> None:
    op.create_table(
        "asset_search",
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("described", sa.Text(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("ocr_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("transcript_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("path_words", sa.Text(), nullable=False, server_default=""),
        sa.Column("snippet", sa.Text(), nullable=True),
    )
    for name, parts in (("everything", (DESCRIBED, NOTE, OCR, TRANSCRIPT, PATH)),
                        ("shown", (DESCRIBED, OCR, TRANSCRIPT)),
                        ("shown_capped", (DESCRIBED, OCR))):
        op.execute(f"ALTER TABLE asset_search ADD COLUMN {name} tsvector GENERATED ALWAYS AS ({_words(*parts)}) STORED")
        op.execute(f"CREATE INDEX ix_asset_search_{name} ON asset_search USING gin ({name})")

    op.execute(
        "ALTER TABLE video_scenes ADD COLUMN words tsvector GENERATED ALWAYS AS ("
        "setweight(to_tsvector('english', coalesce(description, '')), 'A') || "
        "setweight(jsonb_to_tsvector('english', coalesce(tags, '[]'::jsonb), '[\"string\"]'), 'B')) STORED"
    )
    op.execute("CREATE INDEX ix_video_scenes_words ON video_scenes USING gin (words)")
    op.drop_column("video_scenes", "search_synced_at")

    op.create_table(
        "transcript_segments",
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.execute("ALTER TABLE transcript_segments ADD COLUMN words tsvector "
               "GENERATED ALWAYS AS (to_tsvector('english', text)) STORED")
    op.execute("CREATE INDEX ix_transcript_segments_words ON transcript_segments USING gin (words)")

    op.execute("UPDATE assets SET search_synced_at = NULL, transcript_synced_at = NULL")
    op.execute("DELETE FROM system_metadata WHERE key LIKE 'search.%'")


def downgrade() -> None:
    op.drop_table("transcript_segments")
    op.execute("DROP INDEX IF EXISTS ix_video_scenes_words")
    op.execute("ALTER TABLE video_scenes DROP COLUMN IF EXISTS words")
    op.add_column("video_scenes", sa.Column("search_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_table("asset_search")
    op.execute("UPDATE assets SET search_synced_at = NULL, transcript_synced_at = NULL")
