"""Add performance indexes on hot columns: worker_jobs, search_sync_queue, assets.

These indexes improve query performance for the most frequently executed queries
in the job scheduling and search sync pipelines.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-03-21
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_jobs_type_status "
        "ON worker_jobs (job_type, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_worker_jobs_claimed_lease "
        "ON worker_jobs (lease_expires_at) WHERE status = 'claimed'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_sync_queue_status "
        "ON search_sync_queue (status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_assets_deleted_at "
        "ON assets (deleted_at) WHERE deleted_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_type_status")
    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_claimed_lease")
    op.execute("DROP INDEX IF EXISTS idx_search_sync_queue_status")
    op.execute("DROP INDEX IF EXISTS idx_assets_deleted_at")
