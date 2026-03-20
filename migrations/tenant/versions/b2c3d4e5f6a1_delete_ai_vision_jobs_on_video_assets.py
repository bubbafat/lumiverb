"""Delete failed ai_vision worker_jobs on video assets.

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f0
Create Date: 2026-03-20

ai_vision only processes image assets; video assets use video-vision instead.
Before media_type filtering was added to the enqueue path, some video assets
were incorrectly enqueued for ai_vision and those jobs all failed. They show
up permanently as failed in the pipeline status display but can never be
retried (the eligibility guard blocks them). Delete them.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "b2c3d4e5f6a1"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        text("""
            DELETE FROM worker_jobs
            WHERE job_type = 'ai_vision'
              AND status = 'failed'
              AND asset_id IN (
                  SELECT asset_id FROM assets
                  WHERE media_type = 'video'
              )
        """)
    )


def downgrade() -> None:
    # Data-only migration: cannot reverse.
    pass
