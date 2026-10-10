"""Scene descriptions by Detail (Low / Normal / High) instead of scene knobs.

Revision ID: f3a9d2c6e8b1
Revises: e7b2c9d4a1f3
Create Date: 2026-10-10

- artifact_lineage.level: the level an artifact was made at, for a producer
  with a level setting (scenes' Detail, its index: low 0, normal 1, high 2),
  kept apart from the settings hash. Existing rows have none (scenes made by
  version 1, stale anyway).
- video_index_chunks loses anchor_phash and scene_start_ms: chunks are whole
  scene windows now, so a scene never carries over from one to the next.
- A video whose scenes were under way (not found yet) starts over: its
  chunks were cut 30 s at a time and its scenes found the old way. Their
  scenes aren't described yet (a video is described once found), and
  scenes hold no human data.
- The scene knobs an account changed (change needed, longest and shortest
  scene, frame width) are dropped with them: Detail replaces them.

Down: the columns come back empty; levels and the dropped knobs don't.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a9d2c6e8b1"
down_revision: Union[str, Sequence[str], None] = "e7b2c9d4a1f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifact_lineage", sa.Column("level", sa.SmallInteger(), nullable=True))
    op.drop_column("video_index_chunks", "anchor_phash")
    op.drop_column("video_index_chunks", "scene_start_ms")
    op.execute(
        "DELETE FROM video_scenes s USING assets a"
        " WHERE a.asset_id = s.asset_id AND NOT COALESCE(a.video_indexed, false)"
    )
    op.execute(
        "DELETE FROM video_index_chunks c USING assets a"
        " WHERE a.asset_id = c.asset_id AND NOT COALESCE(a.video_indexed, false)"
    )
    op.execute("DELETE FROM system_metadata WHERE key = 'producer.scenes'")


def downgrade() -> None:
    op.add_column("video_index_chunks", sa.Column("scene_start_ms", sa.Integer(), nullable=True))
    op.add_column("video_index_chunks", sa.Column("anchor_phash", sa.String(), nullable=True))
    op.drop_column("artifact_lineage", "level")
