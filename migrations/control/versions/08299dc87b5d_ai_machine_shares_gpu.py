"""An AI machine can share the GPU the scheduler decodes video on (ADR-016 phase 4).

Robert's call (Oct 9): video work comes first on that GPU. While the
scheduler decodes video there, a machine marked as sharing it gets fewer
AI requests, down to none. The built-in machine (the scheduler's own
computer) always shares it.

Revision ID: 08299dc87b5d
Revises: b6591b39778f
Create Date: 2026-10-09
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "08299dc87b5d"
down_revision: Union[str, Sequence[str], None] = "b6591b39778f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_machines", sa.Column("shares_gpu", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("ai_machines", "shares_gpu")
