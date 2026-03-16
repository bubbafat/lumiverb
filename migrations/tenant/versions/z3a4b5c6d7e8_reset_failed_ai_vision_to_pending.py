"""Reset failed ai_vision jobs to pending for eligible image assets.

Revision ID: z3a4b5c6d7e8
Revises: y2z3a4b5c6d7
Create Date: 2026-03-16

Data fix: --retry-failed was returning 0 for ai_vision because the eligibility
query excluded assets with any historical completed job record. Assets re-run
with --force accumulate old completed rows that now block retry_failed from
resetting a newer failed job.

This migration resets failed ai_vision jobs to pending for image assets where:
  - the job is in 'failed' status
  - the asset has proxy_key set (proxy is ready)
  - the asset is an image (media_type LIKE 'image/%')
  - there is no active (pending/claimed) job already running

Blocked jobs (fail_count >= 3) are intentionally excluded — only --force
can recover those.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "z3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "y2z3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        text(
            """
            UPDATE worker_jobs wj
            SET status      = 'pending',
                worker_id         = NULL,
                claimed_at        = NULL,
                lease_expires_at  = NULL,
                completed_at      = NULL,
                error_message     = NULL
            FROM assets a
            WHERE a.asset_id        = wj.asset_id
              AND wj.job_type       = 'ai_vision'
              AND wj.status         = 'failed'
              AND wj.fail_count     < 3
              AND a.proxy_key       IS NOT NULL
              AND a.media_type      LIKE 'image/%'
              AND NOT EXISTS (
                SELECT 1 FROM worker_jobs w2
                WHERE w2.asset_id = wj.asset_id
                  AND w2.job_type = 'ai_vision'
                  AND w2.status IN ('pending', 'claimed')
              )
            """
        )
    )


def downgrade() -> None:
    # Data-only migration: cannot reliably reverse (original error state is gone).
    pass
