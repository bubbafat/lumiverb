"""Lumiverb's own files kept by their names (ADR-024 phase 1, part 4).

Revision ID: a3c9e5f1b7d2
Revises: f2a6c8e4b0d7
Create Date: 2026-10-19

A clip's files are kept and sent by their names (docs/data-names.md), as
its artifacts are since part 3: an open upload's file (`uploads.artifact`,
and its CHECK) and each missing-file report waiting for upkeep's repair
(system_metadata's `artifacts.missing`, `<asset_id>:<file>`): `proxy`
becomes `lumiverb.preview`, `video_preview` `lumiverb.video.preview`, and so
on. Nothing else keeps a file's kind: storage keys are paths, and the
clip's columns (`proxy_key`, ...) are its own.

The mapping is written here, never imported (src/producers/names.py RENAMED
goes in part 5): an install that skips releases, a restored backup and a
downgrade still run this migration. tests/test_migration_files_named.py
holds it equal to RENAMED while both exist.

Down: each renamed back, by the same mapping; a word that isn't one of
these stays as it is.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c9e5f1b7d2"
down_revision: str | Sequence[str] | None = "f2a6c8e4b0d7"
branch_labels = None
depends_on = None

# Each file's kind before part 4, and its name.
FILES: dict[str, str] = {
    "proxy": "lumiverb.preview",
    "thumbnail": "lumiverb.preview.thumbnail",
    "video_preview": "lumiverb.video.preview",
    "analysis_proxy": "lumiverb.video.analysis_proxy",
    "scene_rep": "lumiverb.video.scenes.picture",
}
# What's sent in chunks (src/server/uploads.py KINDS): the CHECK on uploads.artifact.
UPLOADED = ("video_preview", "analysis_proxy")
MISSING = "artifacts.missing"


def _check(names: Sequence[str]) -> str:
    return "artifact IN (" + ", ".join(f"'{n}'" for n in names) + ")"


def _uploads(conn, way: dict[str, str], allowed: Sequence[str]) -> None:
    op.drop_constraint("uploads_artifact", "uploads", type_="check")
    for old, new in way.items():
        conn.execute(sa.text("UPDATE uploads SET artifact = :new WHERE artifact = :old"), {"new": new, "old": old})
    op.create_check_constraint("uploads_artifact", "uploads", _check(allowed))


def _reports(conn, way: dict[str, str]) -> None:
    """Each waiting report's file renamed, the list kept in its order (an entry not read as one stays)."""
    raw = conn.execute(sa.text("SELECT value FROM system_metadata WHERE key = :k"), {"k": MISSING}).scalar()
    try:
        entries = json.loads(raw) if raw else None
    except ValueError:
        return
    if not isinstance(entries, list):
        return

    def renamed(entry):
        if not isinstance(entry, str):
            return entry
        asset_id, colon, file = entry.rpartition(":")
        return f"{asset_id}:{way[file]}" if colon and file in way else entry

    new = [renamed(e) for e in entries]
    if new != entries:
        conn.execute(sa.text("UPDATE system_metadata SET value = :v WHERE key = :k"),
                     {"v": json.dumps(new), "k": MISSING})


def upgrade() -> None:
    conn = op.get_bind()
    _uploads(conn, {old: FILES[old] for old in UPLOADED}, [FILES[old] for old in UPLOADED])
    _reports(conn, FILES)


def downgrade() -> None:
    conn = op.get_bind()
    _uploads(conn, {FILES[old]: old for old in UPLOADED}, UPLOADED)
    _reports(conn, {new: old for old, new in FILES.items()})
