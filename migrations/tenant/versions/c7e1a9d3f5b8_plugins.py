"""Plug-ins an account turns on, and what its admins accepted (ADR-021).

Revision ID: c7e1a9d3f5b8
Revises: b5d9e3f7a1c4
Create Date: 2026-10-18

- plugins: each plug-in's switch in this account (no row: off), and the
  version of its acknowledgment accepted when it was turned on.
- plugin_acknowledgments: every acceptance, kept for good (who accepted
  which version of a plug-in's text, and when).

Nothing is turned on by this migration.

Down: both go.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e1a9d3f5b8"
down_revision: str | Sequence[str] | None = "b5d9e3f7a1c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugins",
        sa.Column("plugin_id", sa.Text(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("acknowledged_version", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "plugin_acknowledgments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("plugin_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_plugin_acknowledgments_plugin_id", "plugin_acknowledgments", ["plugin_id"])


def downgrade() -> None:
    op.drop_index("ix_plugin_acknowledgments_plugin_id", table_name="plugin_acknowledgments")
    op.drop_table("plugin_acknowledgments")
    op.drop_table("plugins")
