"""Scene numbers once per clip; crashes counted per clip.

Revision ID: f3c4d5e6a7b8
Revises: e8a1b2c3d4f5
Create Date: 2026-10-09

video_scenes: the server numbers a clip's scenes as each chunk completes;
a clip whose scene detection resumed numbered them from 0 again. They're
numbered again in time order here, then (asset_id, scene_index) is unique.

producer_crashes: how many times in a row the scheduler tried a clip's
artifact and the job crashed without charging it (runners.py). After
CRASHES_BEFORE_CHARGE of them it's charged as a failure, so back-off and
give-up apply; a success or a charged failure clears it.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3c4d5e6a7b8"
down_revision: Union[str, Sequence[str], None] = "e8a1b2c3d4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "UPDATE video_scenes s SET scene_index = n.i FROM ("
        "  SELECT scene_id, row_number() OVER (PARTITION BY asset_id ORDER BY start_ms, scene_id) - 1 AS i"
        "  FROM video_scenes) n"
        " WHERE n.scene_id = s.scene_id AND s.scene_index <> n.i"))
    op.create_unique_constraint("uq_video_scenes_asset_scene_index", "video_scenes", ["asset_id", "scene_index"])
    op.create_table(
        "producer_crashes",
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("artifact", sa.String(), primary_key=True),
        sa.Column("crashes", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("producer_crashes")
    op.drop_constraint("uq_video_scenes_asset_scene_index", "video_scenes", type_="unique")
