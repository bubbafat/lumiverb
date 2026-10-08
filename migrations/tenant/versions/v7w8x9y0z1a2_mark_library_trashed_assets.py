"""Clips trashed with their library say so.

Revision ID: v7w8x9y0z1a2
Revises: u6v7w8x9y0z1
Create Date: 2026-10-07

Trashing a library soft-deletes its assets without a reason, which reads
as "the scanner found the file missing" and promises the clip comes back
when the file does. Libraries have no restore, so it doesn't. Library
trash now records deleted_reason = 'library'; this marks the clips of
libraries already in the trash the same way.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "v7w8x9y0z1a2"
down_revision: Union[str, Sequence[str], None] = "u6v7w8x9y0z1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "UPDATE assets SET deleted_reason = 'library'"
        " WHERE deleted_at IS NOT NULL AND deleted_reason IS NULL"
        "   AND library_id IN (SELECT library_id FROM libraries WHERE status = 'trashed')"
    ))


def downgrade() -> None:
    op.execute(sa.text("UPDATE assets SET deleted_reason = NULL WHERE deleted_reason = 'library'"))
