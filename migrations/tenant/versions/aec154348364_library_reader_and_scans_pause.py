"""Each library names its reader and has its own Scans pause (ADR-018 phase 1).

Revision ID: aec154348364
Revises: b3d5f7a9c1e4
Create Date: 2026-10-13

- libraries.reader: who reads its files (src/shared/readers.py); "server",
  this server, for every library until devices come.
- libraries.scans_paused_at / scans_paused_by: its Scans pause, which was
  the account's Scans switch (producer_pauses target "scans"). An account
  whose switch was paused has every library paused, by whoever paused it,
  then the switch's row goes: kept until replaced (ADR-016 principle 11).
  Pause all (target "all") stays the account's.

Down: a library paused pauses the account's Scans switch again (the
earliest pause's who and when), then the columns go.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aec154348364"
down_revision: str | Sequence[str] | None = "b3d5f7a9c1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("libraries", sa.Column("reader", sa.String(), nullable=False, server_default="server"))
    op.add_column("libraries", sa.Column("scans_paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("libraries", sa.Column("scans_paused_by", sa.String(), nullable=True))
    op.execute(
        "UPDATE libraries l SET scans_paused_at = p.paused_at, scans_paused_by = p.paused_by"
        " FROM producer_pauses p WHERE p.target = 'scans' AND p.scope = 'work'"
    )
    op.execute("DELETE FROM producer_pauses WHERE target = 'scans' AND scope = 'work'")


def downgrade() -> None:
    op.execute(
        "INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
        " SELECT 'scans', 'work', scans_paused_by, scans_paused_at FROM libraries"
        " WHERE scans_paused_at IS NOT NULL ORDER BY scans_paused_at LIMIT 1"
        " ON CONFLICT (target, scope) DO NOTHING"
    )
    op.drop_column("libraries", "scans_paused_by")
    op.drop_column("libraries", "scans_paused_at")
    op.drop_column("libraries", "reader")
