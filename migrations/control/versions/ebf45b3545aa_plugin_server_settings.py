"""The server's settings for the plug-ins installed on it (ADR-023 phase 1c).

Revision ID: ebf45b3545aa
Revises: f7c3a1e9b5d2
Create Date: 2026-10-18

- plugin_server_settings: the values (chosen) the server admin set, by key, for one
  plug-in's settings of the server's scope (its manifest's: a mirror to
  download from, a key for a service the whole install uses), each checked
  by its schema before it was saved; a setting with no value has its
  default. Who changed them last, and when.

Down: it goes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "ebf45b3545aa"
down_revision: str | Sequence[str] | None = "f7c3a1e9b5d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_server_settings",
        sa.Column("plugin_id", sa.Text(), primary_key=True),
        sa.Column("chosen", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("changed_by", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("plugin_server_settings")
