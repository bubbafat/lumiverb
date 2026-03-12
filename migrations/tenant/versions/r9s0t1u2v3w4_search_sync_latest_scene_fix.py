"""Fix search_sync_latest view to deduplicate on (asset_id, scene_id).

The previous view used DISTINCT ON (asset_id) which collapsed all scene-level
sync rows for an asset into a single row, making scene syncs invisible to
claim_batch. This migration updates the view to treat asset rows (scene_id IS NULL)
and scene rows (scene_id IS NOT NULL) as distinct entities.

Revision ID: r9s0t1u2v3w4
Revises: k1l2m3n4o5p6
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "r9s0t1u2v3w4"
down_revision = "k1l2m3n4o5p6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP VIEW IF EXISTS search_sync_latest"))
    conn.execute(
        text(
            """
            CREATE VIEW search_sync_latest AS
            SELECT DISTINCT ON (asset_id, scene_id)
                asset_id,
                scene_id,
                sync_id,
                status,
                operation,
                created_at,
                processing_started_at
            FROM search_sync_queue
            ORDER BY asset_id, scene_id, created_at DESC
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP VIEW IF EXISTS search_sync_latest"))
    conn.execute(
        text(
            """
            CREATE OR REPLACE VIEW search_sync_latest AS
            SELECT DISTINCT ON (asset_id)
                asset_id,
                sync_id,
                status,
                operation,
                created_at,
                processing_started_at
            FROM search_sync_queue
            ORDER BY asset_id, created_at DESC
            """
        )
    )

