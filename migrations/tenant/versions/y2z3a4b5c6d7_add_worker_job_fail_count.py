"""add_worker_job_fail_count

Adds fail_count to worker_jobs to track consecutive failures per job.
Once fail_count reaches FAILURE_BLOCK_THRESHOLD (3), the job transitions
to 'blocked' status and is excluded from automatic retries until --force.

Revision ID: y2z3a4b5c6d7
Revises: x1y2z3a4b5c6
Create Date: 2026-03-16

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "y2z3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "x1y2z3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "worker_jobs",
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("worker_jobs", "fail_count")
