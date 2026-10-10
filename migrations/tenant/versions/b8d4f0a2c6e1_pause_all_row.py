"""Pause all is a row of its own (ADR-016 phase 4, ADR-017 phase 3).

Revision ID: b8d4f0a2c6e1
Revises: 4e3bcb0d9be8
Create Date: 2026-10-10

Until now Pause all paused each switch there was, one row each, so a
producer a later version added (capture, location) ran in an account
someone had paused. Now Pause all is the row ("all", "work") and covers
every switch, present and future (repository/lineage.py pauses()). An
account whose every switch of that time is paused was paused with Pause
all (or switch by switch to the same end): it gets the row, so it stays
paused. Its switch rows stay as they are.

Down: the row goes, and each switch of that time is paused by a row of
its own, as before.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f0a2c6e1"
down_revision: Union[str, Sequence[str], None] = "4e3bcb0d9be8"
branch_labels = None
depends_on = None

# Every pause switch before this revision (pause_targets() then).
SWITCHES = ("scans", "upkeep", "probe", "analysis_proxy", "scenes", "scene_vision", "vision", "ocr", "clip",
            "faces", "transcript")


def upgrade() -> None:
    op.execute(sa.text(
        "INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
        " SELECT 'all', 'work',"
        "   (SELECT paused_by FROM producer_pauses WHERE scope = 'work' AND target = ANY(:s)"
        "    ORDER BY paused_at, target LIMIT 1),"
        "   (SELECT min(paused_at) FROM producer_pauses WHERE scope = 'work' AND target = ANY(:s))"
        " WHERE (SELECT count(*) FROM producer_pauses WHERE scope = 'work' AND target = ANY(:s)) = :n"
        " ON CONFLICT (target, scope) DO NOTHING"
    ).bindparams(sa.bindparam("s", list(SWITCHES), type_=sa.ARRAY(sa.String())), n=len(SWITCHES)))


def downgrade() -> None:
    op.execute(sa.text(
        "INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
        " SELECT t, 'work', a.paused_by, a.paused_at FROM producer_pauses a, unnest(CAST(:s AS text[])) t"
        " WHERE a.target = 'all' AND a.scope = 'work'"
        " ON CONFLICT (target, scope) DO NOTHING"
    ).bindparams(sa.bindparam("s", list(SWITCHES), type_=sa.ARRAY(sa.String()))))
    op.execute(sa.text("DELETE FROM producer_pauses WHERE target = 'all' AND scope = 'work'"))
