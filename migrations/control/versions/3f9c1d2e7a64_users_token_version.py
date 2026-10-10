"""Add users.token_version: bumping it revokes every token the user holds.

Revision ID: 3f9c1d2e7a64
Revises: 08299dc87b5d
Create Date: 2026-10-09

A JWT carries the version it was issued at ("tv"); the middleware and the
refresh endpoint refuse one that doesn't match. Changing a user's role or
resetting their password bumps it; deleting the user removes the row.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "3f9c1d2e7a64"
down_revision: Union[str, Sequence[str], None] = "08299dc87b5d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("users", "token_version")
