"""Pausing processing (Robert, Oct 9).

Revision ID: a1839b2c9c58
Revises: e517f1428b9b
Create Date: 2026-10-09

An admin can pause all of the account's processing, or one producer's:
the scheduler starts nothing more of it, and what's running finishes.
processing_paused has a row for each: target 'all', or the producer's
artifact. (Stopping a producer's redo alone stays producer_redo_paused.)
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1839b2c9c58"
down_revision: Union[str, Sequence[str], None] = "e517f1428b9b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processing_paused",
        sa.Column("target", sa.String(), primary_key=True),
        sa.Column("paused_by", sa.Text(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("processing_paused")
