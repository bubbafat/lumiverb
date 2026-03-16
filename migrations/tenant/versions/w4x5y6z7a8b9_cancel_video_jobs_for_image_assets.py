"""Cancel video-preview and video-vision jobs incorrectly enqueued for image assets.

Revision ID: w4x5y6z7a8b9
Revises: v3w4x5y6z7a8
Create Date: 2026-03-16

Data fix: a bug in the pipeline supervisor caused video-preview (and potentially
video-vision) jobs to be enqueued for image assets (e.g. ARW, JPEG) after proxy
completed. The enqueue command lacked a media-type guard for these job types,
which is now fixed in query_for_enqueue. This migration cancels any pending or
failed jobs of those types that were created for non-video assets.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "w4x5y6z7a8b9"
down_revision: Union[str, Sequence[str], None] = "v3w4x5y6z7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        text(
            """
            UPDATE worker_jobs
            SET status = 'cancelled'
            WHERE job_type IN ('video-preview', 'video-vision')
              AND status IN ('pending', 'failed')
              AND asset_id IN (
                SELECT asset_id FROM assets WHERE media_type != 'video'
              )
            """
        )
    )


def downgrade() -> None:
    # Data-only migration: cannot reliably reverse (original bad rows are gone).
    pass
