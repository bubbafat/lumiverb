"""add idx_worker_jobs_asset_type_created

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-03-10

Adds index on (asset_id, job_type, created_at DESC) for efficient
latest-per-(asset_id, job_type) queries in pipeline_status.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "j0k1l2m3n4o5"
down_revision: Union[str, Sequence[str], None] = "i9j0k1l2m3n4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_jobs_asset_type_created
        ON worker_jobs (asset_id, job_type, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_worker_jobs_asset_type_created")
