"""Faces in video: where in the video a face was seen.

Revision ID: b5d1e8a3c7f2
Revises: f3a9d2c6e8b1
Create Date: 2026-10-11

- faces.frame_ms: for a face found in a video, the time (ms) of the frame
  its box and crop are from. NULL for a photo's.
- faces.seen_ms: every time (ms) in the video the face was seen at, in
  order (the frames looked at it was found in). NULL for a photo's.

A person seen across a video's frames is one face (src/producers/video_faces),
so these say where in the video they are.

Down: the columns go, with the video faces' times.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5d1e8a3c7f2"
down_revision: Union[str, Sequence[str], None] = "f3a9d2c6e8b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("faces", sa.Column("frame_ms", sa.Integer(), nullable=True))
    op.add_column("faces", sa.Column("seen_ms", sa.ARRAY(sa.Integer()), nullable=True))


def downgrade() -> None:
    op.drop_column("faces", "seen_ms")
    op.drop_column("faces", "frame_ms")
