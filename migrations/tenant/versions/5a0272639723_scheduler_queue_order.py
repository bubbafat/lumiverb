"""The scheduler's queue reads each kind's due clips oldest first (ADR-016 phase 4).

An index on the order clips were added, among those in sight, so the
queue stops at its limit instead of sorting every clip each time.

Revision ID: 5a0272639723
Revises: fbd7f3be1cb0
Create Date: 2026-10-09
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "5a0272639723"
down_revision: Union[str, Sequence[str], None] = "fbd7f3be1cb0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_assets_queue_order ON assets (created_at, asset_id)"
               " WHERE deleted_at IS NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_assets_queue_order")
