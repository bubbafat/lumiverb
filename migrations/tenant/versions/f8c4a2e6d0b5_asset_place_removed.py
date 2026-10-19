"""The built-in Place names' names on clips go (ADR-023 phase 3, part 4: the removal).

Revision ID: f8c4a2e6d0b5
Revises: e6b2d8f4a1c3
Create Date: 2026-10-19

asset_place, each clip's nearest town and landmark as the built-in Place
names producer named them, has been written by nothing since part 3 and
shown only until the GeoNames plug-in's facts for the clip were saved. It
goes: a name is made again from the clip's location and GeoNames' list by
the plug-in's work, and nothing a person made is in it (their locations,
their places and their places' credits are asset_location's, and stay).

Down: it's made again, empty.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8c4a2e6d0b5"
down_revision: str | Sequence[str] | None = "e6b2d8f4a1c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS asset_place")


def downgrade() -> None:
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
        sa.Column("landmark_id", sa.Text(), nullable=True),
        sa.Column("landmark_name", sa.Text(), nullable=True),
        sa.Column("landmark_label", sa.Text(), nullable=True),
        sa.Column("landmark_distance_m", sa.Integer(), nullable=True),
    )
