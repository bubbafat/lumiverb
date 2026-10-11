"""Face detection by level (Off / Low / Normal / High) instead of gates one by one.

Revision ID: c8e2f4a6b1d9
Revises: b5d1e8a3c7f2
Create Date: 2026-10-11

- Photos' faces found with the gates the account had (its overrides over
  the defaults of then) are current at Normal under the new settings'
  hash: nothing is found again for the change. Ones found otherwise were
  stale before and stay so.
- The gates an account changed one by one (confidence, sizes, sharpness,
  the image size looked at) are dropped: the level replaces them. The
  grouping settings (merge_close_clusters, merge_distance) stay.
- Videos' faces: their level's index moves up one (Off is 0 now).

Down: photos' faces current at Normal go back to the old defaults' hash;
videos' levels move down one. Dropped gates don't come back.
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8e2f4a6b1d9"
down_revision: Union[str, Sequence[str], None] = "b5d1e8a3c7f2"
branch_labels = None
depends_on = None

# The faces producer's output-affecting settings before levels, and after.
OLD_DEFAULTS = {"model": "buffalo_l", "det_size": 640, "max_detect_edge": 1280, "min_confidence": 0.5,
                "min_area_fraction": 0.003, "min_face_pixels": 40, "min_relative_size": 0.15, "min_sharpness": 15.0}
GATES = ("max_detect_edge", "min_confidence", "min_area_fraction", "min_face_pixels", "min_relative_size",
         "min_sharpness")
NEW_SETTINGS = {"model": "buffalo_l", "det_size": 640}
NORMAL = 2  # its index among off, low, normal, high


def _hash(settings: dict) -> str:
    """src/shared/producers.settings_hash, as it is now."""
    canonical = json.dumps(dict(settings), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def upgrade() -> None:
    conn = op.get_bind()
    raw = conn.execute(sa.text("SELECT value FROM system_metadata WHERE key = 'producer.faces'")).scalar()
    overrides = json.loads(raw) if raw else {}
    then = {**OLD_DEFAULTS, **{k: v for k, v in overrides.items() if k in GATES}}
    conn.execute(sa.text(
        "UPDATE artifact_lineage SET settings_hash = :new, level = :normal"
        " WHERE artifact = 'faces' AND producer = 'insightface' AND producer_version = '1'"
        "   AND settings_hash = :then"), {"new": _hash(NEW_SETTINGS), "normal": NORMAL, "then": _hash(then)})
    kept = {k: v for k, v in overrides.items() if k not in GATES}
    if kept != overrides:
        if kept:
            conn.execute(sa.text("UPDATE system_metadata SET value = :v, updated_at = now()"
                                 " WHERE key = 'producer.faces'"), {"v": json.dumps(kept, sort_keys=True)})
        else:
            conn.execute(sa.text("DELETE FROM system_metadata WHERE key = 'producer.faces'"))
    conn.execute(sa.text("UPDATE artifact_lineage SET level = level + 1"
                         " WHERE artifact = 'video_faces' AND level IS NOT NULL"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE artifact_lineage SET settings_hash = :old, level = NULL"
        " WHERE artifact = 'faces' AND producer = 'insightface' AND settings_hash = :new AND level = :normal"),
        {"old": _hash(OLD_DEFAULTS), "new": _hash(NEW_SETTINGS), "normal": NORMAL})
    conn.execute(sa.text("UPDATE artifact_lineage SET level = level - 1"
                         " WHERE artifact = 'video_faces' AND level > 0"))
