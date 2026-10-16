"""Content hashes name their scheme, and the columns say what they hold (src/shared/content_hash.py).

Revision ID: f4c2a9e7b1d3
Revises: e2b7d4f9a1c6
Create Date: 2026-10-16

Every content hash is "scheme:value" now: a whole file's SHA-256 is
"sha256:" and its 64 hex digits, as a bigger file's was already "fp1:" and
its. So a hash says how it was made, and the columns that hold one are
named for what they hold, not for one way of making it:

- assets.sha256, ignored_files.sha256, scan_found.sha256 → content_hash;
- artifact_lineage.source_sha256 → source_hash (and a redo's run,
  video_index_chunks.run, says source_hash too);
- scan_found.legacy_sha256 → previous_hash.

Each bare SHA-256 gets "sha256:" (hex digits in either case, lowercased);
an fp1 stays as it is. A value that is neither, which no hasher of ours
made, is no content hash at all: it's dropped (NULL, content not known), as
it could never have matched one. A check on each column then holds every
value to the grammar, whatever its scheme. active_assets is made again: a
view keeps the column names it was made with.

The artifacts' own checksums (proxy_sha256, thumbnail_sha256,
analysis_proxy_sha256, rep_frame_sha256) are SHA-256s of Lumiverb's own
files, not content hashes: they stay.

Down: the old names, and each "sha256:" taken off again.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4c2a9e7b1d3"
down_revision: str | Sequence[str] | None = "e2b7d4f9a1c6"
branch_labels = None
depends_on = None

_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"
# The grammar (src/shared/content_hash.py GRAMMAR).
GRAMMAR = "^[a-z0-9]{1,32}:[A-Za-z0-9._~-]{1,128}$"

# (table, old column, new column)
_RENAMED = (
    ("assets", "sha256", "content_hash"),
    ("ignored_files", "sha256", "content_hash"),
    ("scan_found", "sha256", "content_hash"),
    ("scan_found", "legacy_sha256", "previous_hash"),
    ("artifact_lineage", "source_sha256", "source_hash"),
)


def _labelled(column: str) -> str:
    """SQL: the column's value with its scheme: a bare SHA-256 as "sha256:", a
    value already in the grammar (an fp1) as it is, anything else NULL."""
    return (f"CASE WHEN {column} ~ '^[0-9A-Fa-f]{{64}}$' THEN 'sha256:' || lower({column})"
            f" WHEN {column} ~ '{GRAMMAR}' THEN {column} END")


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    # A path's file deleted for good is remembered once by its content: of two
    # that would be the same once labelled, the first is kept.
    op.execute(sa.text(
        "DELETE FROM ignored_files i USING ignored_files j"
        " WHERE j.library_id = i.library_id AND j.rel_path = i.rel_path AND j.ignored_id < i.ignored_id"
        f"   AND COALESCE({_labelled('i.sha256')}, '') = COALESCE({_labelled('j.sha256')}, '')"))
    for table, old, new in _RENAMED:
        op.alter_column(table, old, new_column_name=new)
        op.execute(sa.text(f"UPDATE {table} SET {new} = {_labelled(new)}"
                           f" WHERE {new} IS NOT NULL AND {new} !~ '^fp1:[0-9a-f]{{64}}$'"))
        op.create_check_constraint(f"ck_{table}_{new}", table, f"{new} ~ '{GRAMMAR}'")
    op.execute(sa.text(_VIEW_DDL))
    # A redo's run is the lineage it was sent with.
    op.execute(sa.text(
        "UPDATE video_index_chunks SET run = (run - 'source_sha256')"
        "   || jsonb_build_object('source_hash', CASE WHEN run->>'source_sha256' ~ '^[0-9A-Fa-f]{64}$'"
        "                                         THEN 'sha256:' || lower(run->>'source_sha256')"
        f"                                         WHEN run->>'source_sha256' ~ '{GRAMMAR}'"
        "                                         THEN run->>'source_sha256' END)"
        " WHERE run ? 'source_sha256'"))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    for table, old, new in _RENAMED:
        op.drop_constraint(f"ck_{table}_{new}", table, type_="check")
        op.execute(sa.text(f"UPDATE {table} SET {new} = substr({new}, 8) WHERE {new} LIKE 'sha256:%'"))
        op.alter_column(table, new, new_column_name=old)
    op.execute(sa.text(_VIEW_DDL))
    op.execute(sa.text(
        "UPDATE video_index_chunks SET run = (run - 'source_hash')"
        "   || jsonb_build_object('source_sha256', CASE WHEN run->>'source_hash' LIKE 'sha256:%'"
        "                                           THEN substr(run->>'source_hash', 8)"
        "                                           ELSE run->>'source_hash' END)"
        " WHERE run ? 'source_hash'"))
