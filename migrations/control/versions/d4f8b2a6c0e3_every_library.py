"""Whether a person sees every library (ADR-018 decision 8, phase 8).

Revision ID: d4f8b2a6c0e3
Revises: c9e5a7b3d1f4
Create Date: 2026-10-17

users.every_library: true, everyone's until an admin says otherwise, gives
them their account role on every library but the ones set otherwise for
them (the account database's library_roles) and the private ones; false,
they see only the libraries they're given a role on. An account admin sees
every library either way.

Down: the column goes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8b2a6c0e3"
down_revision: str | Sequence[str] | None = "c9e5a7b3d1f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("every_library", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("users", "every_library")
