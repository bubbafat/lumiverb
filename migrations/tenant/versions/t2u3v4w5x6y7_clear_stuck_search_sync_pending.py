"""Clear stuck pending rows in search_sync_queue.

Revision ID: t2u3v4w5x6y7
Revises: s1t2u3v4w5x6
Create Date: 2026-03-13

Data fix only: pending rows for assets that already have a synced row are
hidden by search_sync_latest (latest created_at wins). Mark those pending
rows as synced so they are no longer stuck and pending_count matches the view.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "t2u3v4w5x6y7"
down_revision: Union[str, Sequence[str], None] = "s1t2u3v4w5x6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        text(
            """
            UPDATE search_sync_queue
            SET status = 'synced'
            WHERE status = 'pending'
            AND asset_id IN (
                SELECT asset_id FROM search_sync_queue WHERE status = 'synced'
            )
            """
        )
    )


def downgrade() -> None:
    # Data-only migration: cannot reliably reverse (would recreate stuck state).
    pass
