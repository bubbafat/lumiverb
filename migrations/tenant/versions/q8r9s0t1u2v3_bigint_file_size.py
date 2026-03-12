"""Change assets.file_size from INTEGER to BIGINT

Revision ID: q8r9s0t1u2v3
Revises: p7q8r9s0t1u2
Create Date: 2026-03-12
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "q8r9s0t1u2v3"
down_revision: Union[str, Sequence[str], None] = "p7q8r9s0t1u2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE assets ALTER COLUMN file_size TYPE BIGINT")


def downgrade() -> None:
    op.execute("ALTER TABLE assets ALTER COLUMN file_size TYPE INTEGER")

