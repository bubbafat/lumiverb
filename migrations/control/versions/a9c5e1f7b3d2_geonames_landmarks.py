"""GeoNames' landmarks beside its towns (ADR-021's Place names, Robert 2026-10-18).

Revision ID: a9c5e1f7b3d2
Revises: f3b7d1e5a9c2
Create Date: 2026-10-18

- geonames_landmarks: the features of src/plugins/geonames/landmarks.py's
  kinds with a Wikipedia article (some kinds every one): name (folded and
  GeoNames' ASCII spelling, as the towns', searched by name), point, kind,
  region and country. By latitude, for the nearest one to a point.
- plugin_data.version: which version of a plug-in's data was fetched
  (PluginSpec.data_version); NULL for what was fetched before, version 1. A
  server whose data is older fetches it again, using what it has meanwhile.

Nothing is fetched by this migration.

Down: both go.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c5e1f7b3d2"
down_revision: str | Sequence[str] | None = "f3b7d1e5a9c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plugin_data", sa.Column("version", sa.Integer(), nullable=True))
    op.create_table(
        "geonames_landmarks",
        sa.Column("geonameid", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ascii_name", sa.Text(), nullable=False),
        sa.Column("folded", sa.Text(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("feature_code", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=False),
        sa.Column("admin1_code", sa.Text(), nullable=False),
    )
    op.execute("CREATE INDEX ix_geonames_landmarks_lat ON geonames_landmarks (lat)")
    op.execute("CREATE INDEX ix_geonames_landmarks_folded ON geonames_landmarks (folded text_pattern_ops)")
    op.execute("CREATE INDEX ix_geonames_landmarks_ascii_name"
               " ON geonames_landmarks (lower(ascii_name) text_pattern_ops)")


def downgrade() -> None:
    op.drop_table("geonames_landmarks")
    op.drop_column("plugin_data", "version")
