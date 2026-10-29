"""Scenes' CLIP vectors: each scene's picture's vector (video_scenes.likeness) and the model it's from (likeness_model).

Revision ID: c4e8a2d6f0b3
Revises: b5d1f7a3c9e2
Create Date: 2026-10-28

Made by the lumiverb.video.scene_likeness producer (src/producers/scene_likeness/) with the model photos' vectors
are made with, so a scene is compared as a photo is. Dimensionless, as asset_embeddings.embedding_vector is: the
model it's from says how long. Every scene is without one until it's embedded; going back down drops them (they're
made again, from the analysis proxies, once it's up again).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a2d6f0b3"
down_revision: str | Sequence[str] | None = "b5d1f7a3c9e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE video_scenes ADD COLUMN likeness vector"))
    op.add_column("video_scenes", sa.Column("likeness_model", sa.Text(), nullable=True))


def downgrade() -> None:
    # Their lineage goes with them: the release before has no such producer, and nothing it made is left.
    op.execute(sa.text("DELETE FROM artifact_lineage WHERE artifact = 'lumiverb.video.scene_likeness'"))
    op.drop_column("video_scenes", "likeness_model")
    op.drop_column("video_scenes", "likeness")
