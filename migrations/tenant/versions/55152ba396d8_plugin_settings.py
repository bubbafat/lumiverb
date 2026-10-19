"""Each account's settings for the plug-ins the server admin installed (ADR-023 phase 1c).

Revision ID: 55152ba396d8
Revises: e5b9f3a7c1d4
Create Date: 2026-10-18

- plugin_settings: the values (chosen) this account's admins set, by key, for one
  plug-in's settings of the account's scope (its manifest's), each checked
  by its schema before it was saved; a setting with no value has its
  default. Who changed them last, and when.

Down: it goes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "55152ba396d8"
down_revision: str | Sequence[str] | None = "e5b9f3a7c1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_settings",
        sa.Column("plugin_id", sa.Text(), primary_key=True),
        sa.Column("chosen", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("changed_by", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("plugin_settings")
