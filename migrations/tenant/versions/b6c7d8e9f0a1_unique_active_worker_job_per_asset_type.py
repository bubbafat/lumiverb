"""Add partial unique index: one active job per (job_type, asset_id).

Prevents the TOCTOU race where two concurrent chunk completions both see
all_chunks_complete=True and has_pending_job=False, creating duplicate
video-vision (or other) jobs for the same asset.

The index covers only pending and claimed rows so that historical completed,
failed, cancelled, and blocked rows are unaffected.

Revision ID: b6c7d8e9f0a1
Revises: 95408ad1a2b3
Create Date: 2026-03-21
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, Sequence[str], None] = "95408ad1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Cancel duplicate active jobs before adding the unique constraint.
    # Keeps the best row per (job_type, asset_id): prefer 'claimed' over
    # 'pending', then most-recently created.  Any extras are cancelled so
    # they fall outside the partial index predicate and do not block it.
    op.execute(
        """
        UPDATE worker_jobs
        SET status = 'cancelled'
        WHERE job_id IN (
            SELECT job_id FROM (
                SELECT job_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY job_type, asset_id
                           ORDER BY
                               CASE status WHEN 'claimed' THEN 0 ELSE 1 END,
                               created_at DESC
                       ) AS rn
                FROM worker_jobs
                WHERE status = 'pending' OR status = 'claimed'
            ) ranked
            WHERE rn > 1
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_worker_jobs_one_active_per_type_asset
        ON worker_jobs (job_type, asset_id)
        WHERE status = 'pending' OR status = 'claimed'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_worker_jobs_one_active_per_type_asset")
