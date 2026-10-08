"""Assets: analysis proxy (ADR-016 phase 2).

A full-length, low-resolution copy of each video, with audio, kept on the
brain so transcription, scenes and vision can run while the storage holding
the originals sleeps.

Revision ID: w8x9y0z1a2b3
Revises: v7w8x9y0z1a2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "w8x9y0z1a2b3"
down_revision: str | Sequence[str] | None = "v7w8x9y0z1a2"
branch_labels = None
depends_on = None

_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.add_column("assets", sa.Column("analysis_proxy_key", sa.String(), nullable=True))
    op.add_column("assets", sa.Column("analysis_proxy_sha256", sa.String(), nullable=True))
    op.add_column("assets", sa.Column("analysis_proxy_generated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("assets", "analysis_proxy_generated_at")
    op.drop_column("assets", "analysis_proxy_sha256")
    op.drop_column("assets", "analysis_proxy_key")
    op.execute(sa.text(_VIEW_DDL))
