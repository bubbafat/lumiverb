"""A clip's landmark beside its town (ADR-021's Place names, Robert 2026-10-18).

Revision ID: e5b9f3a7c1d4
Revises: d4a8e2c6f0b3
Create Date: 2026-10-18

asset_place gains the landmark the location is at (GeoNames', within its
kind's reach, src/plugins/geonames/landmarks.py), named with the town by the
same producer: landmark_id ("geonames:<id>"), its name, its label (name,
region, country, for when no town is near) and how far. NULL: at none.
Every clip already named is named again (the producer's version 2).

Down: the columns go.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b9f3a7c1d4"
down_revision: str | Sequence[str] | None = "d4a8e2c6f0b3"
branch_labels = None
depends_on = None

COLUMNS = (("landmark_id", sa.Text()), ("landmark_name", sa.Text()), ("landmark_label", sa.Text()),
           ("landmark_distance_m", sa.Integer()))


def upgrade() -> None:
    for name, kind in COLUMNS:
        op.add_column("asset_place", sa.Column(name, kind, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column("asset_place", name)
