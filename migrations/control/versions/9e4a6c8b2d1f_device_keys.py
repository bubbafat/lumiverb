"""A device's key (ADR-018 decision 8, phase 5).

Revision ID: 9e4a6c8b2d1f
Revises: 8d107a0b4312
Create Date: 2026-10-17

A key can be a device's: a computer that scans the libraries it reads, not
a person. Its device_id names the device (the account's devices table) and
its role is "device", no person's role; the API lets it scan its libraries
and nothing else (src/server/api/devices.py). A device's key has its device
and every other key has none; a device has one live key at most.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9e4a6c8b2d1f"
down_revision: str | Sequence[str] | None = "8d107a0b4312"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("device_id", sa.Text(), nullable=True))
    op.create_index("api_keys_device_id", "api_keys", ["device_id"], unique=True,
                    postgresql_where=sa.text("device_id IS NOT NULL AND revoked_at IS NULL"))
    op.create_check_constraint("api_keys_device", "api_keys", "(device_id IS NULL) = (role <> 'device')")


def downgrade() -> None:
    # A device's key has no meaning before devices: they go.
    op.execute("DELETE FROM api_keys WHERE device_id IS NOT NULL")
    op.drop_constraint("api_keys_device", "api_keys", type_="check")
    op.drop_index("api_keys_device_id", table_name="api_keys")
    op.drop_column("api_keys", "device_id")
