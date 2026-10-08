"""Find a library's active assets by their content (follow moves and renames).

When a file goes missing, the archive looks for an empty, newer copy of it in
the same library (copy, then delete the original): by SHA-256 among the
active assets. Index only those, so archiving a whole folder stays cheap
however big the library.

Revision ID: ef56ab12cd34
Revises: ab12cd34ef56
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ef56ab12cd34"
down_revision: str | Sequence[str] | None = "ab12cd34ef56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "CREATE INDEX ix_assets_active_sha ON assets (library_id, sha256) "
        "WHERE deleted_at IS NULL AND sha256 IS NOT NULL"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_assets_active_sha"))
