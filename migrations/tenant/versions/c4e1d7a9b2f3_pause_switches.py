"""Pause switches (Robert, Oct 9, after #50).

Revision ID: c4e1d7a9b2f3
Revises: a1839b2c9c58
Create Date: 2026-10-09

Nothing stores "pause all" any more: each switch (Scans, Upkeep, each
producer the scheduler makes) is paused on its own in processing_paused,
and the account's state is derived from them. An old 'all' row becomes
every switch paused, each keeping who paused it and when (a switch already
paused keeps its own). The switches are named as of this migration; the
table itself is unchanged. Going down leaves the switches as they are.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e1d7a9b2f3"
down_revision: Union[str, Sequence[str], None] = "a1839b2c9c58"
branch_labels = None
depends_on = None

# pause_targets() as of this migration.
SWITCHES = ("scans", "upkeep", "probe", "analysis_proxy", "scenes", "scene_vision", "vision", "ocr", "clip",
            "faces", "transcript")


def upgrade() -> None:
    op.get_bind().execute(sa.text(
        "INSERT INTO processing_paused (target, paused_by, paused_at)"
        " SELECT s, a.paused_by, a.paused_at FROM processing_paused a, unnest(CAST(:s AS text[])) AS s"
        " WHERE a.target = 'all' ON CONFLICT (target) DO NOTHING"), {"s": list(SWITCHES)})
    op.execute("DELETE FROM processing_paused WHERE target = 'all'")


def downgrade() -> None:
    pass
