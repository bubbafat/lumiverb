"""Archive and trash, Robert's model (Oct 8).

Revision ID: b7e4c2a9d013
Revises: ef56ab12cd34
Create Date: 2026-10-08

Archive = out of sight, kept forever; trash = deleted for good after the
account's trash days (30 unless an admin changes it).

- libraries.trashed_at: when a library went in the trash, so its days can
  be counted. Libraries already there get now.

Clips deleted before reasons were recorded aren't converted: Robert starts
the data fresh once this lands (his call, Oct 8).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4c2a9d013"
down_revision: Union[str, Sequence[str], None] = "ef56ab12cd34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("libraries", sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("UPDATE libraries SET trashed_at = now() WHERE status = 'trashed'"))


def downgrade() -> None:
    op.drop_column("libraries", "trashed_at")
