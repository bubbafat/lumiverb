"""Scenes found again beside a video's scenes, not in their place.

Revision ID: d4f7a2c9e1b3
Revises: c8e2f4a6b1d9
Create Date: 2026-10-12

- video_scenes.staged: NULL for the scenes a video shows; 'finding' for
  scenes a redo is finding beside them; 'found' once that run is found and
  merged, while they're described. The last one described swaps them in.
- video_index_chunks.run: how a set of chunks finds its scenes (the
  lineage it records), so a redo resumes its own run and records it.
- A video's scene numbers are unique within each of those sets.

Down: scenes not swapped in yet are dropped (their run starts over).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "d4f7a2c9e1b3"
down_revision: Union[str, Sequence[str], None] = "c8e2f4a6b1d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("video_scenes", sa.Column("staged", sa.Text(), nullable=True))
    op.create_check_constraint("ck_video_scenes_staged", "video_scenes", "staged IN ('finding', 'found')")
    op.add_column("video_index_chunks", sa.Column("run", JSONB(), nullable=True))
    op.drop_constraint("uq_video_scenes_asset_scene_index", "video_scenes", type_="unique")
    op.execute("ALTER TABLE video_scenes ADD CONSTRAINT uq_video_scenes_asset_scene_index"
               " UNIQUE NULLS NOT DISTINCT (asset_id, staged, scene_index)")


def downgrade() -> None:
    op.execute("DELETE FROM video_scenes WHERE staged IS NOT NULL")
    op.execute("DELETE FROM video_index_chunks WHERE asset_id IN"
               " (SELECT asset_id FROM assets WHERE video_indexed)"
               " AND status <> 'completed'")
    op.drop_constraint("uq_video_scenes_asset_scene_index", "video_scenes", type_="unique")
    op.create_unique_constraint("uq_video_scenes_asset_scene_index", "video_scenes", ["asset_id", "scene_index"])
    op.drop_column("video_index_chunks", "run")
    op.drop_constraint("ck_video_scenes_staged", "video_scenes", type_="check")
    op.drop_column("video_scenes", "staged")
