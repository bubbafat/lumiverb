"""Add partial unique index on search_sync_queue to prevent TOCTOU duplicate enqueues.

The index covers only pending and processing rows so that historical synced rows
are unaffected. The enqueue() method uses ON CONFLICT DO NOTHING against this index
for atomic deduplication without a SELECT-then-INSERT race.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-03-21
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ssq_pending_asset_scene
        ON search_sync_queue (asset_id, COALESCE(scene_id, ''))
        WHERE status IN ('pending', 'processing')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_ssq_pending_asset_scene")
