"""Library changes reported by the machine that holds the storage (ADR-016 phase 2).

The brain mounts the DAS over the network and can't watch it, so the Mac
reports paths it sees change. Each row is one path waiting to be scanned;
`version` grows on every report so an acknowledgement only clears what
the scan actually saw.

Revision ID: x9y0z1a2b3c4
Revises: w8x9y0z1a2b3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "x9y0z1a2b3c4"
down_revision: str | Sequence[str] | None = "w8x9y0z1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("CREATE SEQUENCE library_changes_version_seq"))
    op.create_table(
        "library_changes",
        sa.Column("change_id", sa.String(), primary_key=True),
        sa.Column(
            "library_id",
            sa.String(),
            sa.ForeignKey("libraries.library_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rel_path", sa.String(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "version",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('library_changes_version_seq')"),
        ),
        sa.UniqueConstraint("library_id", "rel_path", name="uq_library_changes_library_path"),
    )
    op.create_index("ix_library_changes_library_id", "library_changes", ["library_id"])


def downgrade() -> None:
    op.drop_index("ix_library_changes_library_id", table_name="library_changes")
    op.drop_table("library_changes")
    op.execute(sa.text("DROP SEQUENCE IF EXISTS library_changes_version_seq"))
