"""add processing_started_at lease column for search_sync_queue

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-03-11

Adds:
- Column processing_started_at to search_sync_queue for lease tracking
- Resets any existing rows stuck in status='processing' back to 'pending'
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "k1l2m3n4o5p6"
down_revision: Union[str, Sequence[str], None] = "j0k1l2m3n4o5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    # Replace existing view so we can safely add the new column and expose it.
    conn.execute(text("DROP VIEW IF EXISTS search_sync_latest"))
    conn.execute(
        text(
            """
            ALTER TABLE search_sync_queue
                ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ
            """
        )
    )
    conn.execute(
        text(
            """
            UPDATE search_sync_queue
            SET status = 'pending',
                processing_started_at = NULL
            WHERE status = 'processing'
            """
        )
    )
    # Update view to expose processing_started_at for lease logic
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


def downgrade() -> None:
    conn = op.get_bind()
    # Drop the current view definition that references processing_started_at.
    conn.execute(text("DROP VIEW IF EXISTS search_sync_latest"))
    # Restore the legacy view definition that does not reference processing_started_at.
    conn.execute(
        text(
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
    )
    # Then drop the lease column from the base table.
    conn.execute(
        text(
            """
            ALTER TABLE search_sync_queue
                DROP COLUMN IF EXISTS processing_started_at
            """
        )
    )

