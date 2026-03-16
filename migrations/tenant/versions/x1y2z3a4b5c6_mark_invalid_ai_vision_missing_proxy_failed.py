"""mark_invalid_ai_vision_missing_proxy_failed

Revision ID: x1y2z3a4b5c6
Revises: w4x5y6z7a8b9
Create Date: 2026-03-16

"""

from typing import Sequence, Union

from alembic import op


revision: str = "x1y2z3a4b5c6"
down_revision: Union[str, Sequence[str], None] = "w4x5y6z7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ERR = "Invalid ai_vision job: missing proxy_key (video proxy deferred or proxy not ready)"


def upgrade() -> None:
    """
    Mark pending/claimed ai_vision worker_jobs as failed when they cannot run:
    - asset has no proxy_key, or
    - asset is not an image (video pipeline uses video-vision).

    This prevents the vision worker from repeatedly crashing with:
      ValueError: No proxy_key in ai_vision job for asset ...

    Marking them failed (vs cancelled) keeps them visible for investigation and
    allows optional retry workflows once assets become eligible.
    """
    op.execute(
        f"""
        UPDATE worker_jobs wj
        SET status = 'failed',
            error_message = '{_ERR}',
            completed_at = NOW()
        FROM assets a
        WHERE a.asset_id = wj.asset_id
          AND wj.job_type = 'ai_vision'
          AND wj.status IN ('pending', 'claimed')
          AND (
            a.proxy_key IS NULL
            OR a.media_type NOT LIKE 'image/%'
          )
        """
    )


def downgrade() -> None:
    """
    Restore affected worker_jobs back to pending (best-effort reversal).
    """
    op.execute(
        f"""
        UPDATE worker_jobs
        SET status = 'pending',
            error_message = NULL,
            completed_at = NULL
        WHERE job_type = 'ai_vision'
          AND status = 'failed'
          AND error_message = '{_ERR}'
        """
    )

