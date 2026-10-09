"""A path holds one clip in sight; archived ones keep theirs (Robert, Oct 9).

"A binary different file with the same name is a completely new file", and
the old one goes missing: archived where it was, so the same path can name
a clip in sight and archived ones. (library_id, rel_path) is unique only
among clips in sight (deleted_at IS NULL).

Downgrading fails once any path holds more than one clip (a file changed
since this ran): those have to be deleted for good, or moved, first.

Revision ID: b77f2a03eee2
Revises: 0c663a6f08e1
Create Date: 2026-10-09
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "b77f2a03eee2"
down_revision: Union[str, Sequence[str], None] = "0c663a6f08e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_assets_library_rel_path", "assets", type_="unique")
    op.execute("CREATE UNIQUE INDEX uq_assets_library_rel_path_in_sight ON assets (library_id, rel_path)"
               " WHERE deleted_at IS NULL")
    # Looking a path up still finds its archived clips quickly.
    op.execute("CREATE INDEX ix_assets_library_rel_path ON assets (library_id, rel_path)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_assets_library_rel_path")
    op.execute("DROP INDEX IF EXISTS uq_assets_library_rel_path_in_sight")
    op.create_unique_constraint("uq_assets_library_rel_path", "assets", ["library_id", "rel_path"])
