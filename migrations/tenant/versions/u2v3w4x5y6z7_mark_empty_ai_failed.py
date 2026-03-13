"""mark_empty_ai_failed

Revision ID: u2v3w4x5y6z7
Revises: t2u3v4w5x6y7
Create Date: 2026-03-13

"""

from typing import Sequence, Union

from alembic import op


revision: str = "u2v3w4x5y6z7"
down_revision: Union[str, Sequence[str], None] = "t2u3v4w5x6y7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Mark completed ai_vision worker_jobs as failed when the corresponding
    asset_metadata has empty description and tags. This lets --retry-failed
    pick them up. The pipeline uses worker_jobs.status, not assets.status.
    """
    op.execute(
        """
        UPDATE worker_jobs wj
        SET status = 'failed', error_message = 'Empty AI description and tags from vision worker'
        FROM asset_metadata m
        WHERE wj.asset_id = m.asset_id
          AND wj.job_type = 'ai_vision'
          AND wj.status = 'completed'
          AND (m.data->>'description' IS NULL OR btrim(m.data->>'description') = '')
          AND (
              m.data->'tags' IS NULL
              OR jsonb_typeof(m.data->'tags') != 'array'
              OR jsonb_array_length(m.data->'tags') = 0
          )
        """
    )


def downgrade() -> None:
    """
    Restore worker_jobs we changed back to completed.
    """
    op.execute(
        """
        UPDATE worker_jobs
        SET status = 'completed', error_message = NULL
        WHERE job_type = 'ai_vision'
          AND status = 'failed'
          AND error_message = 'Empty AI description and tags from vision worker'
        """
    )

