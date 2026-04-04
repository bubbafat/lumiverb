"""Add has_text flag to asset_metadata JSONB data.

Revision ID: n4o5p6q7r8s9
Revises: m3n4o5p6q7r9
Create Date: 2026-04-04

Backfills has_text boolean in asset_metadata.data based on existing ocr_text:
- ocr_text is non-empty string → has_text = true
- ocr_text is empty string → has_text = false
- ocr_text is NULL / missing → no change (NULL means "never checked")
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "n4o5p6q7r8s9"
down_revision: Union[str, None] = "m3n4o5p6q7r9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Non-empty ocr_text → has_text = true
    op.execute(
        """
        UPDATE asset_metadata
        SET data = data || '{"has_text": true}'::jsonb
        WHERE data->>'ocr_text' IS NOT NULL
          AND data->>'ocr_text' != ''
        """
    )
    # Empty ocr_text → has_text = false
    op.execute(
        """
        UPDATE asset_metadata
        SET data = data || '{"has_text": false}'::jsonb
        WHERE data->>'ocr_text' IS NOT NULL
          AND data->>'ocr_text' = ''
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE asset_metadata
        SET data = data - 'has_text'
        WHERE data ? 'has_text'
        """
    )
