"""add search_sync_latest view and idx_ssq_asset_created

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-03-10

Adds:
- Index idx_ssq_asset_created on (asset_id, created_at DESC) for efficient latest-per-asset queries
- View search_sync_latest: one row per asset with the most recent queue state
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "i9j0k1l2m3n4"
down_revision: Union[str, Sequence[str], None] = "h8i9j0k1l2m3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ssq_asset_created
        ON search_sync_queue (asset_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE OR REPLACE VIEW search_sync_latest AS
        SELECT DISTINCT ON (asset_id)
            asset_id,
            sync_id,
            status,
            operation,
            created_at
        FROM search_sync_queue
        ORDER BY asset_id, created_at DESC
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS search_sync_latest")
    op.execute("DROP INDEX IF EXISTS idx_ssq_asset_created")
