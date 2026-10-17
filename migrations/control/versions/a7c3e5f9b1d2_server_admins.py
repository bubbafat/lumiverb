"""The server admin is a person (ADR-018 decision 8, phase 5).

Revision ID: a7c3e5f9b1d2
Revises: 9e4a6c8b2d1f
Create Date: 2026-10-17

What belongs to the install rather than an account (its accounts, where
previews live, the network, backups, logs) is a server admin's: a person who
signs in, users.server_admin. Until now every account admin had those
settings, so where the install has one account its admins become server
admins and nothing changes for them (a Mac's, a one-account Linux install's).
With several accounts none does: the machine's owner names one
(`lumiverb-server server-admins add EMAIL`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e5f9b1d2"
down_revision: str | Sequence[str] | None = "9e4a6c8b2d1f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("server_admin", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("""
        UPDATE users SET server_admin = true
        WHERE role = 'admin'
          AND (SELECT count(*) FROM tenants WHERE status = 'active') = 1
          AND tenant_id = (SELECT tenant_id FROM tenants WHERE status = 'active')
    """)


def downgrade() -> None:
    op.drop_column("users", "server_admin")
