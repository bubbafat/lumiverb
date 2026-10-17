"""The account's devices (ADR-018 decision 8, phase 5).

Revision ID: c6d2e8a4f1b7
Revises: b3e7c1f9d2a5
Create Date: 2026-10-17

A device is a computer that scans the libraries it reads, registered by an
account admin: its name, for people ("Robert's Mac Studio"), when and by
whom it was registered. Its key is in the control database's api_keys,
by device_id, so a new key or none changes nothing here. A library a
device reads names it as its reader (libraries.reader).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6d2e8a4f1b7"
down_revision: str | Sequence[str] | None = "b3e7c1f9d2a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "devices",
        sa.Column("device_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("registered_by", sa.Text(), nullable=True),
        sa.CheckConstraint("device_id ~ '^dev_[0-9A-Z]{26}$'", name="devices_id"),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="devices_name"),
    )


def downgrade() -> None:
    op.drop_table("devices")
