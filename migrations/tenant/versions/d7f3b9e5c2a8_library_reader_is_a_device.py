"""A library's reader is this server or a registered device (ADR-018 phase 6).

Revision ID: d7f3b9e5c2a8
Revises: c6d2e8a4f1b7
Create Date: 2026-10-17

libraries.reader names who reads its files: 'server' (this server), or a
device's id (the account's devices). Held to those two forms here; that the
device is registered is the API's to check (PUT /v1/libraries/{id}/reader),
and a device isn't removed while a library names it (routers/devices.py).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d7f3b9e5c2a8"
down_revision: str | Sequence[str] | None = "c6d2e8a4f1b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint("libraries_reader", "libraries", "reader = 'server' OR reader ~ '^dev_[0-9A-Z]{26}$'")


def downgrade() -> None:
    op.execute("UPDATE libraries SET reader = 'server' WHERE reader <> 'server'")
    op.drop_constraint("libraries_reader", "libraries", type_="check")
