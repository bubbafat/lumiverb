"""Live Photos: Apple's identifier of the Live Photo a photo or its moving half is part of (assets.live_photo_id).

Revision ID: b5d1f7a3c9e2
Revises: d9a3f5c1e7b4
Create Date: 2026-10-21

The photo and its .mov carry the same identifier (the photo's Apple maker notes, the video's QuickTime keys);
a video whose identifier a photo of its library has is that photo's motion, out of sight as part of it
(deleted_reason 'live_photo': src/server/repository/live_photos.py). Read by the scan from here on, and for
the clips there already by the Live Photos producer (src/producers/live_photo/), which reads only clips
beside a file of the other kind with the same name: the index on each clip's name without its extension
(live_photos.STEM, the same expression) finds those.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d1f7a3c9e2"
down_revision: str | Sequence[str] | None = "d9a3f5c1e7b4"
branch_labels = None
depends_on = None

# active_assets is SELECT * FROM assets, expanded when the view was made: rebuilt around the column change.
_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.add_column("assets", sa.Column("live_photo_id", sa.String(), nullable=True))
    op.execute(sa.text(_VIEW_DDL))
    op.execute(sa.text(
        "CREATE INDEX ix_assets_live_photo ON assets (library_id, live_photo_id) WHERE live_photo_id IS NOT NULL"))
    op.execute(sa.text(
        r"CREATE INDEX ix_assets_stem ON assets (library_id, lower(regexp_replace(rel_path, '\.[^./]*$', '')))"))


def downgrade() -> None:
    # Its motions back in sight first: without the identifier nothing says what they're part of.
    op.execute(sa.text(
        "UPDATE assets SET deleted_at = NULL, deleted_reason = NULL, search_synced_at = NULL"
        " WHERE deleted_reason = 'live_photo'"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_assets_stem"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_assets_live_photo"))
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("assets", "live_photo_id")
    op.execute(sa.text(_VIEW_DDL))
