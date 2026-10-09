"""A path holds one clip in sight; archived ones keep theirs (Robert, Oct 9).

"A binary different file with the same name is a completely new file", and
the old one goes missing: archived where it was, so the same path can name
a clip in sight and archived ones. (library_id, rel_path) is unique only
among clips in sight (deleted_at IS NULL).

An emptied-trash record names a file at a path, so a path can have several
(versions deleted for good over time): one row per (library, path, content),
a row without a SHA-256 naming whatever is at the path. Each keeps the
file's size and time, so a scan hashes a file there only when they differ.

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
    op.execute("ALTER TABLE ignored_files DROP CONSTRAINT ignored_files_pkey")
    op.execute("ALTER TABLE ignored_files ADD COLUMN ignored_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY")
    op.execute("CREATE UNIQUE INDEX uq_ignored_files_file ON ignored_files (library_id, rel_path, COALESCE(sha256, ''))")
    # Its size and time when it was deleted: a scan hashes the file at that path
    # only when they differ, not every file a person removed on every scan.
    op.execute("ALTER TABLE ignored_files ADD COLUMN file_size BIGINT, ADD COLUMN file_mtime TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE ignored_files DROP COLUMN file_size, DROP COLUMN file_mtime")
    op.execute("DROP INDEX IF EXISTS uq_ignored_files_file")
    # One per path again: the most recent (one emptying records several at once).
    op.execute("DELETE FROM ignored_files a USING ignored_files b WHERE a.library_id = b.library_id"
               " AND a.rel_path = b.rel_path AND (a.created_at, a.ignored_id) < (b.created_at, b.ignored_id)")
    op.execute("ALTER TABLE ignored_files DROP COLUMN ignored_id")
    op.execute("ALTER TABLE ignored_files ADD PRIMARY KEY (library_id, rel_path)")
    op.execute("DROP INDEX IF EXISTS ix_assets_library_rel_path")
    op.execute("DROP INDEX IF EXISTS uq_assets_library_rel_path_in_sight")
    op.create_unique_constraint("uq_assets_library_rel_path", "assets", ["library_id", "rel_path"])
