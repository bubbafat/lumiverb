"""Delete orphaned search_sync worker_jobs records.

Revision ID: a1b2c3d4e5f0
Revises: m5n6o7p8q9r0
Create Date: 2026-03-20

The search_sync stage uses its own search_sync_queue table exclusively —
worker_jobs records with job_type='search_sync' are never claimed or processed.
Any such records are orphaned and will stay pending forever, causing the
pipeline status display to show false pending counts alongside the correct
synced counts from search_sync_latest.

Delete all worker_jobs rows with job_type='search_sync'.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "a1b2c3d4e5f0"
down_revision: Union[str, Sequence[str], None] = "m5n6o7p8q9r0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().execute(
        text("DELETE FROM worker_jobs WHERE job_type = 'search_sync'")
    )


def downgrade() -> None:
    # Data-only migration: cannot reverse (records were orphaned garbage).
    pass
