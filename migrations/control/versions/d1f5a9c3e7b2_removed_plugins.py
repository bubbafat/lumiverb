"""Uninstalling a plug-in asks what becomes of what it made (ADR-023, Robert 2026-10-20).

Revision ID: d1f5a9c3e7b2
Revises: b5d9e3a7f1c4
Create Date: 2026-10-20

The server admin chooses, at `lumiverb-server plugins uninstall`: keep what
it made (its facts shown and searched in every account as before, its
settings and its own data kept, so installing it again picks everything
up), put it in the trash (hidden, and kept for 30 days, then deleted;
installing it again before then brings it back), or delete everything now.

- removed_plugins: a plug-in uninstalled with what it made kept or in the
  trash: its row as it was installed (version, manifest, default, what was
  accepted), which (keep or trash), who and when, and for the trash when
  it's deleted for good. installed_plugins holds only what's installed, so
  nothing that runs plug-ins reads a removed one.
- plugin_install_log's action `purge`: a trashed plug-in deleted for good.

Down: the table goes, with what it kept (each removed plug-in's account
rows stay, hidden, as an uninstall left them before), and the purges'
log rows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "d1f5a9c3e7b2"
down_revision: str | Sequence[str] | None = "b5d9e3a7f1c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "removed_plugins",
        sa.Column("plugin_id", sa.Text(), primary_key=True),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("on_by_default", sa.Boolean(), nullable=False),
        sa.Column("acknowledged_version", sa.Text(), nullable=True),
        sa.Column("permissions", JSONB(), nullable=False),
        sa.Column("kept", sa.Text(), nullable=False),
        sa.Column("removed_by", sa.Text(), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kept IN ('keep', 'trash')", name="removed_plugins_kept"),
        sa.CheckConstraint("(kept = 'trash') = (purge_after IS NOT NULL)", name="removed_plugins_purge_after"),
    )
    op.drop_constraint("plugin_install_log_action", "plugin_install_log", type_="check")
    op.create_check_constraint("plugin_install_log_action", "plugin_install_log",
                               "action IN ('install', 'default', 'uninstall', 'failed', 'purge')")


def downgrade() -> None:
    op.execute("DELETE FROM plugin_install_log WHERE action = 'purge'")
    op.drop_constraint("plugin_install_log_action", "plugin_install_log", type_="check")
    op.create_check_constraint("plugin_install_log_action", "plugin_install_log",
                               "action IN ('install', 'default', 'uninstall', 'failed')")
    op.drop_table("removed_plugins")
