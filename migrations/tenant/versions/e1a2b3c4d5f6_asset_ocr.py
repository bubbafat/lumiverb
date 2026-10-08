"""asset_ocr: OCR in a table of its own (ADR-016 phase 3, piece 3).

Revision ID: e1a2b3c4d5f6
Revises: d9f3a6b1c2e8
Create Date: 2026-10-08

OCR used to live inside the description's row (asset_metadata.data:
ocr_text, has_text), so describing a clip again wiped it, and OCR couldn't
run before a description existed. Now it's one row per clip here. The
backfill takes each clip's latest description row that has OCR (a newer
description by another model may not), then removes the OCR keys from the
descriptions so there's one place; those clips' search documents are built
again. Downgrade puts it back into each clip's
latest description row (OCR of a clip with no description is dropped).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1a2b3c4d5f6"
down_revision: Union[str, Sequence[str], None] = "d9f3a6b1c2e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_ocr",
        # Goes with its clip when the clip is deleted for good.
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("has_text", sa.Boolean(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(sa.text(
        "INSERT INTO asset_ocr (asset_id, text, has_text, model_id, generated_at)"
        " SELECT DISTINCT ON (m.asset_id) m.asset_id, COALESCE(m.data->>'ocr_text', ''),"
        "   COALESCE((m.data->>'has_text')::boolean, COALESCE(m.data->>'ocr_text', '') <> ''),"
        "   m.model_id, m.generated_at"
        " FROM asset_metadata m WHERE m.data->>'has_text' IS NOT NULL OR m.data->>'ocr_text' IS NOT NULL"
        " ORDER BY m.asset_id, m.generated_at DESC"
    ))
    # Their search documents were built from the latest description, which
    # may not have carried the OCR: build them again.
    op.execute(sa.text(
        "UPDATE assets SET search_synced_at = NULL WHERE asset_id IN (SELECT asset_id FROM asset_ocr)"
    ))
    op.execute(sa.text(
        "UPDATE asset_metadata SET data = data - 'ocr_text' - 'has_text'"
        " WHERE data ? 'ocr_text' OR data ? 'has_text'"
    ))


def downgrade() -> None:
    op.execute(sa.text(
        "UPDATE asset_metadata m SET data = m.data || jsonb_build_object('has_text', o.has_text)"
        "   || CASE WHEN o.text <> '' THEN jsonb_build_object('ocr_text', o.text) ELSE '{}'::jsonb END"
        " FROM asset_ocr o"
        " WHERE m.asset_id = o.asset_id AND m.metadata_id = ("
        "   SELECT l.metadata_id FROM asset_metadata l WHERE l.asset_id = o.asset_id"
        "   ORDER BY l.generated_at DESC LIMIT 1)"
    ))
    op.drop_table("asset_ocr")
