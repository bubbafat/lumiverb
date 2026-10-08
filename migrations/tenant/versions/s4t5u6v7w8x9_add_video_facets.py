"""Add video_facets: one ffprobe pass per video (ADR-016 phase 1).

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
Create Date: 2026-10-07

Technical facts an editor export needs (frame rate including drop-frame,
start timecode, display dimensions, audio layout). Derived data: re-probing
replaces the row. Rows go with their asset (ON DELETE CASCADE).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "s4t5u6v7w8x9"
down_revision: Union[str, Sequence[str], None] = "r3s4t5u6v7w8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_facets",
        sa.Column(
            "asset_id",
            sa.String(),
            sa.ForeignKey("assets.asset_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("container", sa.String(), nullable=True),
        sa.Column("video_codec", sa.String(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("rotation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("frame_rate_num", sa.Integer(), nullable=True),
        sa.Column("frame_rate_den", sa.Integer(), nullable=True),
        sa.Column("start_timecode", sa.String(), nullable=True),
        sa.Column("drop_frame", sa.Boolean(), nullable=True),
        sa.Column("audio_codec", sa.String(), nullable=True),
        sa.Column("audio_channels", sa.Integer(), nullable=True),
        sa.Column("audio_sample_rate", sa.Integer(), nullable=True),
        sa.Column(
            "probed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("video_facets")
