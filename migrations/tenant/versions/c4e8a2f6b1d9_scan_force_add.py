"""A scan can bring back the files a person removed (lumiverb scan --force-add).

Revision ID: c4e8a2f6b1d9
Revises: aec154348364
Create Date: 2026-10-15

- scan_sessions.force_add: the scan adds every file a person removed as if
  they never had: a clip in the trash or archived comes back into sight with
  its content at its path; a file deleted for good comes back as a new clip,
  and is forgotten as deleted. A last resort, asked for by a person; false for
  every scan before this.

Down: drops the column (no scan can bring removed files back; the forget
route this replaces stays retired).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a2f6b1d9"
down_revision: str | Sequence[str] | None = "aec154348364"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scan_sessions",
                  sa.Column("force_add", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("scan_sessions", "force_add")
