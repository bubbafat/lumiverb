"""Add smart collection support: type and saved_query columns.

Revision ID: p1q2r3s4t5u6
Revises: b12fbc1ce2ea
Create Date: 2026-04-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "p1q2r3s4t5u6"
down_revision: Union[str, Sequence[str], None] = "b12fbc1ce2ea"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("type", sa.String(), server_default="static", nullable=False),
    )
    op.add_column(
        "collections",
        sa.Column("saved_query", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("collections", "saved_query")
    op.drop_column("collections", "type")
