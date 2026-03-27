"""Add search_synced_at to assets and video_scenes

Revision ID: h7i8j9k0l1m2
Revises: g6h7i8j9k0l1
Create Date: 2026-03-27 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "h7i8j9k0l1m2"
down_revision = "g6h7i8j9k0l1"
branch_labels = None
depends_on = None

_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))

    op.add_column("assets", sa.Column("search_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("video_scenes", sa.Column("search_synced_at", sa.DateTime(timezone=True), nullable=True))

    # Backfill from search_sync_queue: if an asset has a 'synced' row, stamp it
    op.execute(sa.text("""
        UPDATE assets a SET search_synced_at = sq.created_at
        FROM (
            SELECT DISTINCT ON (asset_id) asset_id, created_at
            FROM search_sync_queue
            WHERE status = 'synced' AND scene_id IS NULL
            ORDER BY asset_id, created_at DESC
        ) sq
        WHERE a.asset_id = sq.asset_id
          AND a.search_synced_at IS NULL
    """))

    op.execute(sa.text("""
        UPDATE video_scenes vs SET search_synced_at = sq.created_at
        FROM (
            SELECT DISTINCT ON (scene_id) scene_id, created_at
            FROM search_sync_queue
            WHERE status = 'synced' AND scene_id IS NOT NULL
            ORDER BY scene_id, created_at DESC
        ) sq
        WHERE vs.scene_id = sq.scene_id
          AND vs.search_synced_at IS NULL
    """))

    # Index for the maintenance sweep query
    op.create_index("ix_assets_search_synced_at", "assets", ["search_synced_at"])

    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_index("ix_assets_search_synced_at", table_name="assets")
    op.drop_column("video_scenes", "search_synced_at")
    op.drop_column("assets", "search_synced_at")
    op.execute(sa.text(_VIEW_DDL))
