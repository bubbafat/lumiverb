"""Add dismissed column to people table.

Revision ID: j0k1l2m3n4o6
Revises: i9j0k1l2m3n5
Create Date: 2026-03-31

Dismissed people act as face sinks — new similar faces are auto-absorbed
by the upkeep propagation job, but the person is hidden from the UI and
their faces are excluded from clustering.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "j0k1l2m3n4o6"
down_revision: Union[str, Sequence[str], None] = "i9j0k1l2m3n5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("people", sa.Column("dismissed", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade() -> None:
    op.drop_column("people", "dismissed")
