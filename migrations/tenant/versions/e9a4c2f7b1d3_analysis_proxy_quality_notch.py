"""The analysis proxy's quality as a notch (ADR-018 decision 6, phase 7).

Revision ID: e9a4c2f7b1d3
Revises: d7f3b9e5c2a8
Create Date: 2026-10-17

Its CRF (crf, 18 to 40) becomes a notch (quality: good, better, best), in
lineage as the CRF it stands for (good 32, better 28, best 23). What was
made with the default, 28, is current at better as it was: nothing is made
again for the change. An account that changed the CRF has the nearest
notch (a tie goes to the better picture); one whose CRF no notch stands for
(24, say) has what it made made another way than now, so its remakes are
held as a new version's are (producer_pauses, paused_by NULL) until an
admin resumes them or says to keep them.

Down: the notch back to the CRF it stands for (a hold it made stays).
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9a4c2f7b1d3"
down_revision: str | Sequence[str] | None = "d7f3b9e5c2a8"
branch_labels = None
depends_on = None

KEY = "producer.analysis_proxy"
# Each notch's CRF (src/producers/analysis_proxy CRF, as it is now), and the default's.
NOTCHES = {"good": 32, "better": 28, "best": 23}
DEFAULT = "better"


def _overrides(conn) -> dict | None:
    raw = conn.execute(sa.text("SELECT value FROM system_metadata WHERE key = :k"), {"k": KEY}).scalar()
    try:
        values = json.loads(raw) if raw else {}
    except ValueError:
        return None
    return values if isinstance(values, dict) else None


def _store(conn, values: dict) -> None:
    if values:
        conn.execute(sa.text("UPDATE system_metadata SET value = :v, updated_at = now() WHERE key = :k"),
                     {"k": KEY, "v": json.dumps(values, sort_keys=True)})
    else:
        conn.execute(sa.text("DELETE FROM system_metadata WHERE key = :k"), {"k": KEY})


def upgrade() -> None:
    conn = op.get_bind()
    values = _overrides(conn)
    if not values or "crf" not in values:
        return
    crf = values.pop("crf")
    if isinstance(crf, bool) or not isinstance(crf, (int, float)):
        crf = NOTCHES[DEFAULT]
    notch = min(NOTCHES, key=lambda n: (abs(NOTCHES[n] - crf), NOTCHES[n]))
    if notch != DEFAULT:
        values["quality"] = notch
    _store(conn, values)
    if NOTCHES[notch] != crf:
        conn.execute(sa.text(
            "INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
            " VALUES ('analysis_proxy', 'redo', NULL, now()) ON CONFLICT (target, scope) DO NOTHING"))


def downgrade() -> None:
    conn = op.get_bind()
    values = _overrides(conn)
    if not values or "quality" not in values:
        return
    crf = NOTCHES.get(values.pop("quality"), NOTCHES[DEFAULT])
    if crf != NOTCHES[DEFAULT]:
        values["crf"] = crf
    _store(conn, values)
