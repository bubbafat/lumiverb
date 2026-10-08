"""assets.trashed_from: what a clip in a person's trash was before.

Revision ID: c4d8e1f2a7b6
Revises: b7e4c2a9d013
Create Date: 2026-10-08

"archived" or "missing" when it was archived before it was trashed, so
restoring it puts it back in the archive (as a restored project comes back
active or archived as it was); NULL when it was in sight.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d8e1f2a7b6"
down_revision: Union[str, Sequence[str], None] = "b7e4c2a9d013"
branch_labels = None
depends_on = None


# active_assets is SELECT * FROM assets, expanded when the view was made:
# it's rebuilt around the column change, as when deleted_reason came.
_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.add_column("assets", sa.Column("trashed_from", sa.String(), nullable=True))
    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("assets", "trashed_from")
    op.execute(sa.text(_VIEW_DDL))
