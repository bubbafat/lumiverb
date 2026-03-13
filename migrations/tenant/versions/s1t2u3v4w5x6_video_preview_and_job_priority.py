"""Add worker_jobs.priority and asset video preview columns.

Revision ID: s1t2u3v4w5x6
Revises: q8r9s0t1u2v3
Create Date: 2026-03-13

Adds:
- worker_jobs.priority (0=urgent, 10=normal, 20=low)
- assets.video_preview_key
- assets.video_preview_last_accessed_at
- assets.video_preview_generated_at
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "s1t2u3v4w5x6"
down_revision: Union[str, Sequence[str], None] = "q8r9s0t1u2v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Worker job priority: lower values are claimed first.
    op.execute(
        """
        ALTER TABLE worker_jobs
            ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 10
        """
    )

    # Video preview columns on assets.
    op.execute(
        """
        ALTER TABLE assets
            ADD COLUMN IF NOT EXISTS video_preview_key TEXT,
            ADD COLUMN IF NOT EXISTS video_preview_last_accessed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS video_preview_generated_at TIMESTAMPTZ
        """
    )


def downgrade() -> None:
    # Drop video preview columns.
    op.execute(
        """
        ALTER TABLE assets
            DROP COLUMN IF EXISTS video_preview_generated_at,
            DROP COLUMN IF EXISTS video_preview_last_accessed_at,
            DROP COLUMN IF EXISTS video_preview_key
        """
    )

    # Drop priority column from worker_jobs.
    op.execute(
        """
        ALTER TABLE worker_jobs
            DROP COLUMN IF EXISTS priority
        """
    )

