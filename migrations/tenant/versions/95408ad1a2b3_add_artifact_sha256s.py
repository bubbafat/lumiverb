"""Add artifact SHA-256 columns (Phase 1).

Tenant schema additions:
- assets.proxy_sha256 TEXT NULL
- assets.thumbnail_sha256 TEXT NULL
- video_scenes.rep_frame_sha256 TEXT NULL
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "95408ad1a2b3"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("proxy_sha256", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("thumbnail_sha256", sa.Text(), nullable=True))
    op.add_column("video_scenes", sa.Column("rep_frame_sha256", sa.Text(), nullable=True))


_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("video_scenes", "rep_frame_sha256")
    op.drop_column("assets", "thumbnail_sha256")
    op.drop_column("assets", "proxy_sha256")
    op.execute(sa.text(_VIEW_DDL))

