"""Drop scan_status from libraries — always 'idle', never updated.

Revision ID: e5f6g7h8i9j0
Revises: d4e5f6g7h8i9
Create Date: 2026-03-29

The scan_status column was a relic of the old server-side worker queue model.
It was set to 'idle' on creation and never changed. last_scan_at is retained
as it tracks the last ingest time (updated by bump_revision).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e5f6g7h8i9j0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6g7h8i9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("libraries", "scan_status")


def downgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column("scan_status", sa.Text(), nullable=False, server_default="idle"),
    )
