"""A clip's place name: the nearest town to where it was (ADR-021, the Place names plug-in's producer).

Revision ID: d4a8e2c6f0b3
Revises: c7e1a9d3f5b8
Create Date: 2026-10-18

asset_place: one row a clip, made by the `place` producer from the location
the clip shows (a person's, else the file's, else an applied guess), kept
with that location (lat, lon): when the location changes the row no longer
matches, the clip is due again, and the old name stays until the new one is
saved. place_id NULL: no town within the producer's radius.

Down: it goes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4a8e2c6f0b3"
down_revision: str | Sequence[str] | None = "c7e1a9d3f5b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_place",
        sa.Column("asset_id", sa.Text(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("place_id", sa.Text(), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("country_code", sa.Text(), nullable=True),
        sa.Column("distance_m", sa.Integer(), nullable=True),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("named_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("asset_place")
