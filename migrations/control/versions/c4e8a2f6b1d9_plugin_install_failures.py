"""An install that failed, kept with why (ADR-023 phase 3: Lumiverb's own plug-ins).

Revision ID: c4e8a2f6b1d9
Revises: ebf45b3545aa
Create Date: 2026-10-19

- plugin_install_log.why: why an install of one of Lumiverb's own plug-ins
  (src/own_plugins/) failed, its action "failed"; the Admin page says it,
  and it's tried again at the next start. Nothing else has one.

Down: it goes, with the failures it kept.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a2f6b1d9"
down_revision: str | Sequence[str] | None = "ebf45b3545aa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plugin_install_log", sa.Column("why", sa.Text(), nullable=True))
    op.drop_constraint("plugin_install_log_action", "plugin_install_log", type_="check")
    op.create_check_constraint("plugin_install_log_action", "plugin_install_log",
                               "action IN ('install', 'default', 'uninstall', 'failed')")


def downgrade() -> None:
    op.execute("DELETE FROM plugin_install_log WHERE action = 'failed'")
    op.drop_constraint("plugin_install_log_action", "plugin_install_log", type_="check")
    op.create_check_constraint("plugin_install_log_action", "plugin_install_log",
                               "action IN ('install', 'default', 'uninstall')")
    op.drop_column("plugin_install_log", "why")
