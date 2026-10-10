"""Location groundwork (ADR-017 phase 1).

Revision ID: a7c3e9d1f2b4
Revises: f3c4d5e6a7b8
Create Date: 2026-10-09

assets.taken_at_offset_min: the file's OffsetTimeOriginal (or OffsetTime),
in minutes; NULL when it doesn't say. assets.gps_accuracy_m: the file's
GPSHPositioningError. Both are what the file says, written by the scan.

asset_location: one row per clip, only what the file doesn't say: a
person's location, a guess, or a suggestion. The file's GPS stays on
assets.

(0, 0) is a device with no fix, not a place: it's cleared here, and the
scan no longer writes it.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a7c3e9d1f2b4"
down_revision: Union[str, Sequence[str], None] = "f3c4d5e6a7b8"
branch_labels = None
depends_on = None


# active_assets is SELECT * FROM assets, expanded when the view was made:
# it's rebuilt around the column change.
_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.add_column("assets", sa.Column("taken_at_offset_min", sa.Integer(), nullable=True))
    op.add_column("assets", sa.Column("gps_accuracy_m", sa.Float(), nullable=True))
    op.execute(sa.text(_VIEW_DDL))
    op.execute(sa.text("UPDATE assets SET gps_lat = NULL, gps_lon = NULL WHERE gps_lat = 0 AND gps_lon = 0"))
    op.create_table(
        "asset_location",
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("radius_m", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("basis", JSONB(), nullable=False),
        sa.Column("set_by", sa.String(), nullable=True),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("source IN ('person', 'time', 'suggestion')", name="ck_asset_location_source"),
        sa.CheckConstraint("status IN ('applied', 'suggested')", name="ck_asset_location_status"),
    )


def downgrade() -> None:
    op.drop_table("asset_location")
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.drop_column("assets", "gps_accuracy_m")
    op.drop_column("assets", "taken_at_offset_min")
    op.execute(sa.text(_VIEW_DDL))
