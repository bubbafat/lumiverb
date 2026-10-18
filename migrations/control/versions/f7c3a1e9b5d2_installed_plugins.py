"""Plug-ins installed on this server (ADR-023 phase 1a).

Revision ID: f7c3a1e9b5d2
Revises: d1f5b9c3e7a2
Create Date: 2026-10-18

- installed_plugins: each plug-in the server admin installed (a program of
  its own, run outside Lumiverb), its version and manifest as installed, on
  or off by default (in every account that hasn't chosen), the version of
  its acknowledgment the server admin accepted for those accounts, and what
  it reads and reaches as accepted (an update asking more asks again).
- plugin_install_log: every install, change of default and uninstall, who
  and when, kept for good.

Nothing is installed by this migration.

Down: both go.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "f7c3a1e9b5d2"
down_revision: str | Sequence[str] | None = "d1f5b9c3e7a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "installed_plugins",
        sa.Column("plugin_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("on_by_default", sa.Boolean(), nullable=False),
        sa.Column("acknowledged_version", sa.Text(), nullable=True),
        sa.Column("permissions", JSONB(), nullable=False),
        sa.Column("installed_by", sa.Text(), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "plugin_install_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("plugin_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("on_by_default", sa.Boolean(), nullable=True),
        sa.Column("acknowledged_version", sa.Text(), nullable=True),
        sa.Column("permissions", JSONB(), nullable=True),
        sa.Column("by", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('install', 'default', 'uninstall')", name="plugin_install_log_action"),
    )


def downgrade() -> None:
    op.drop_table("plugin_install_log")
    op.drop_table("installed_plugins")
