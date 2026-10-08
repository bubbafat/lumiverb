"""Find an archived asset by its content (the archive model, ADR-016 phase 2).

A file that turns up at a path the library doesn't know is first matched by
SHA-256 against the library's archived (missing) assets. Index only those,
so every new file's lookup is cheap however big the library.

Revision ID: ab12cd34ef56
Revises: x9y0z1a2b3c4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ab12cd34ef56"
down_revision: str | Sequence[str] | None = "x9y0z1a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "CREATE INDEX ix_assets_archived_sha ON assets (library_id, sha256) "
        "WHERE deleted_at IS NOT NULL AND sha256 IS NOT NULL"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_assets_archived_sha"))
