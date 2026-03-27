"""Drop search_sync_queue table and related objects.

Search sync is now handled by timestamp-based inline sync (try_sync_asset)
and periodic maintenance sweep (run_search_sync_sweep) using the
search_synced_at column on assets and video_scenes.

Drops:
- search_sync_latest view
- uq_ssq_pending_asset_scene index
- idx_ssq_asset_created index
- search_sync_queue table
"""

revision = "i8j9k0l1m2n3"
down_revision = "h7i8j9k0l1m2"

from alembic import op
from sqlalchemy import text


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("DROP VIEW IF EXISTS search_sync_latest"))
    conn.execute(text("DROP INDEX IF EXISTS uq_ssq_pending_asset_scene"))
    conn.execute(text("DROP INDEX IF EXISTS idx_ssq_asset_created"))
    conn.execute(text("DROP TABLE IF EXISTS search_sync_queue"))


def downgrade() -> None:
    conn = op.get_bind()
    # Recreate the table
    conn.execute(text("""
        CREATE TABLE search_sync_queue (
            sync_id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL REFERENCES assets(asset_id),
            scene_id TEXT REFERENCES video_scenes(scene_id),
            operation TEXT NOT NULL DEFAULT 'upsert',
            status TEXT NOT NULL DEFAULT 'pending',
            processing_started_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    conn.execute(text("""
        CREATE INDEX idx_ssq_asset_created
        ON search_sync_queue (asset_id, created_at DESC)
    """))
    conn.execute(text("""
        CREATE UNIQUE INDEX uq_ssq_pending_asset_scene
        ON search_sync_queue (asset_id, COALESCE(scene_id, ''))
        WHERE status IN ('pending', 'processing')
    """))
    conn.execute(text("""
        CREATE OR REPLACE VIEW search_sync_latest AS
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
    """))
