"""Preserve human data through re-detection, rescans and re-transcription (ADR-016 phase 0).

Revision ID: r3s4t5u6v7w8
Revises: q2r3s4t5u6v7
Create Date: 2026-10-07

- assets.deleted_reason: why an asset is soft-deleted. 'user' = the user
  trashed it (survives rescans); 'missing' or NULL = the scanner no longer
  found the file (restored when the file reappears).
- assets.transcript_source: who wrote the transcript ('manual' or a
  provider id such as 'whisper'). Machine output never replaces 'manual'.
- ignored_files: files whose trash the user emptied. The asset row is gone,
  but scans and ingest keep skipping the path while the file is on disk.
- face_person_rejections: "this face is not this person", recorded on
  un-assign so auto-assignment never re-applies it.
- Backfill: face matches with no confidence were made by a person (naming
  or dismissing a cluster, assigning a face), not by auto-assignment, so
  mark them confirmed. Re-detection keeps confirmed matches.
- Backfill: rel_path becomes Unicode NFC (the server now stores only NFC),
  except where that would collide with another row of the same library;
  those pairs are left for a person to sort out.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r3s4t5u6v7w8"
down_revision: Union[str, Sequence[str], None] = "q2r3s4t5u6v7"
branch_labels = None
depends_on = None

_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.add_column("assets", sa.Column("deleted_reason", sa.String(), nullable=True))
    op.add_column("assets", sa.Column("transcript_source", sa.String(), nullable=True))
    op.execute(sa.text(_VIEW_DDL))

    op.create_table(
        "ignored_files",
        sa.Column(
            "library_id",
            sa.String(),
            sa.ForeignKey("libraries.library_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("rel_path", sa.String(), primary_key=True),
        sa.Column("sha256", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "face_person_rejections",
        sa.Column(
            "face_id",
            sa.String(),
            sa.ForeignKey("faces.face_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "person_id",
            sa.String(),
            sa.ForeignKey("people.person_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_face_person_rejections_person_id", "face_person_rejections", ["person_id"]
    )

    op.execute(sa.text(
        "UPDATE face_person_matches"
        " SET confirmed = true, confirmed_at = COALESCE(confirmed_at, created_at)"
        " WHERE confirmed = false AND confidence IS NULL"
    ))
    # One pass over the non-NFC rows (set-based, not per row). A row is
    # renamed only if no other row in its library has the same NFC path;
    # renamed rows are re-indexed (Quickwit holds the old path).
    renamed = op.get_bind().execute(sa.text(
        """
        WITH candidates AS (
            SELECT asset_id, library_id, normalize(rel_path, NFC) AS nfc
            FROM assets WHERE rel_path IS NOT NFC NORMALIZED
        ),
        taken AS (
            SELECT a.library_id, normalize(a.rel_path, NFC) AS nfc
            FROM assets a
            JOIN (SELECT DISTINCT library_id, nfc FROM candidates) c
              ON c.library_id = a.library_id AND c.nfc = normalize(a.rel_path, NFC)
            GROUP BY a.library_id, normalize(a.rel_path, NFC)
            HAVING COUNT(*) = 1
        )
        UPDATE assets a
        SET rel_path = c.nfc, search_synced_at = NULL
        FROM candidates c
        JOIN taken t ON t.library_id = c.library_id AND t.nfc = c.nfc
        WHERE a.asset_id = c.asset_id
        """
    )).rowcount
    left = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM assets WHERE rel_path IS NOT NFC NORMALIZED"
    )).scalar()
    if renamed or left:
        import logging

        logging.getLogger("alembic.runtime.migration").warning(
            "rel_path NFC: %d renamed; %d left as they are because another file "
            "in the library has the same name in NFC form",
            renamed, left,
        )
    op.execute(sa.text(
        "UPDATE people p SET confirmation_count = ("
        "  SELECT COUNT(*) FROM face_person_matches m"
        "  WHERE m.person_id = p.person_id AND m.confirmed = true"
        ")"
    ))


def downgrade() -> None:
    op.drop_index("ix_face_person_rejections_person_id", table_name="face_person_rejections")
    op.drop_table("face_person_rejections")
    op.drop_table("ignored_files")
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("assets", "transcript_source")
    op.drop_column("assets", "deleted_reason")
    op.execute(sa.text(_VIEW_DDL))
