"""What each installed plug-in's work gave back for a clip (ADR-023 phase 2).

Revision ID: 87cedeb63a82
Revises: 55152ba396d8
Create Date: 2026-10-18

- plugin_outputs: one row per clip and plug-in: its outputs by key (each a
  fact, {"text", "url"?}, or null: nothing to say), as Lumiverb checked them
  against the plug-in's manifest; the hash of the clip's inputs they were
  made from (src/server/repository/clip_inputs.py: a clip whose inputs
  changed since is due again, and keeps these until the new ones are
  saved); when. Goes with its clip.

Down: it goes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "87cedeb63a82"
down_revision: str | Sequence[str] | None = "55152ba396d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_outputs",
        sa.Column("asset_id", sa.Text(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("plugin_id", sa.Text(), primary_key=True),
        sa.Column("outputs", JSONB(), nullable=False),
        sa.Column("inputs_hash", sa.Text(), nullable=False),
        sa.Column("made_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("plugin_outputs")
