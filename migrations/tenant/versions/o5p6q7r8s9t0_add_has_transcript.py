"""Add has_transcript flag to assets table.

Revision ID: o5p6q7r8s9t0
Revises: n4o5p6q7r8s9
Create Date: 2026-04-06

Three-state flag: NULL = never attempted, false = attempted but no speech
(or no audio track), true = has transcript. Backfills from existing data.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "o5p6q7r8s9t0"
down_revision: Union[str, Sequence[str], None] = "n4o5p6q7r8s9"
branch_labels = None
depends_on = None

_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.add_column("assets", sa.Column("has_transcript", sa.Boolean(), nullable=True))

    # Backfill: transcript_srt present → true
    op.execute(sa.text(
        "UPDATE assets SET has_transcript = true WHERE transcript_srt IS NOT NULL"
    ))
    # Backfill: transcribed_at present but no SRT → false (deleted transcript)
    op.execute(sa.text(
        "UPDATE assets SET has_transcript = false "
        "WHERE transcribed_at IS NOT NULL AND transcript_srt IS NULL"
    ))

    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("assets", "has_transcript")
    op.execute(sa.text(_VIEW_DDL))
