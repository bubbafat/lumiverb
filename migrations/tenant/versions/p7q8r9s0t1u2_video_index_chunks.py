"""video_index_chunks table and extend video_scenes, assets

Revision ID: p7q8r9s0t1u2
Revises: j0k1l2m3n4o5
Create Date: 2026-03-12

Adds:
- video_index_chunks table for chunked video scene indexing
- video_scenes: description, tags, sharpness_score, keep_reason, phash
- assets: duration_sec, video_indexed
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "p7q8r9s0t1u2"
down_revision: Union[str, Sequence[str], None] = "r9s0t1u2v3w4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE video_index_chunks (
            chunk_id        TEXT PRIMARY KEY,
            asset_id        TEXT NOT NULL REFERENCES assets(asset_id),
            chunk_index     INTEGER NOT NULL,
            start_ms        INTEGER NOT NULL,
            end_ms          INTEGER NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            worker_id       TEXT,
            claimed_at      TIMESTAMPTZ,
            lease_expires_at TIMESTAMPTZ,
            completed_at    TIMESTAMPTZ,
            error_message   TEXT,
            anchor_phash    TEXT,
            scene_start_ms  INTEGER,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (asset_id, chunk_index)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_video_index_chunks_asset_status
        ON video_index_chunks (asset_id, status)
        """
    )

    op.execute(
        """
        ALTER TABLE video_scenes
            ADD COLUMN IF NOT EXISTS description TEXT,
            ADD COLUMN IF NOT EXISTS tags JSONB,
            ADD COLUMN IF NOT EXISTS sharpness_score FLOAT,
            ADD COLUMN IF NOT EXISTS keep_reason TEXT,
            ADD COLUMN IF NOT EXISTS phash TEXT
        """
    )

    op.execute(
        """
        ALTER TABLE assets
            ADD COLUMN IF NOT EXISTS duration_sec FLOAT,
            ADD COLUMN IF NOT EXISTS video_indexed BOOLEAN NOT NULL DEFAULT FALSE
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE assets DROP COLUMN IF EXISTS video_indexed")
    op.execute("ALTER TABLE assets DROP COLUMN IF EXISTS duration_sec")

    op.execute("ALTER TABLE video_scenes DROP COLUMN IF EXISTS phash")
    op.execute("ALTER TABLE video_scenes DROP COLUMN IF EXISTS keep_reason")
    op.execute("ALTER TABLE video_scenes DROP COLUMN IF EXISTS sharpness_score")
    op.execute("ALTER TABLE video_scenes DROP COLUMN IF EXISTS tags")
    op.execute("ALTER TABLE video_scenes DROP COLUMN IF EXISTS description")

    op.execute("DROP INDEX IF EXISTS ix_video_index_chunks_asset_status")
    op.execute("DROP TABLE IF EXISTS video_index_chunks")
