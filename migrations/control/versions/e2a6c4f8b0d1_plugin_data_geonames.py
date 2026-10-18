"""What plug-ins keep for the whole server, and GeoNames' places (ADR-021).

Revision ID: e2a6c4f8b0d1
Revises: d4f8b2a6c0e3
Create Date: 2026-10-18

- plugin_data: a plug-in's data for every account, its state (missing,
  fetching with how far, ready with how much, failed with why). Fetched
  when an account turns the plug-in on (src/server/plugin_data.py).
- geonames_places, geonames_admin1, geonames_countries: the Place names
  plug-in's list of towns (GeoNames' cities1000, CC BY 4.0), their regions
  and countries, searched by name: each name folded (lowercase, accents
  dropped: Zürich is "zurich", src/server/repository/places.py fold) and
  GeoNames' own ASCII spelling ("Zuerich").

Nothing is fetched by this migration.

Down: all four go.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2a6c4f8b0d1"
down_revision: str | Sequence[str] | None = "d4f8b2a6c0e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_data",
        sa.Column("plugin_id", sa.Text(), primary_key=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("done", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total", sa.BigInteger(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("count", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("state IN ('fetching', 'ready', 'failed')", name="plugin_data_state"),
    )
    op.create_table(
        "geonames_places",
        sa.Column("geonameid", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ascii_name", sa.Text(), nullable=False),
        sa.Column("folded", sa.Text(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("feature_code", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=False),
        sa.Column("admin1_code", sa.Text(), nullable=False),
        sa.Column("population", sa.BigInteger(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
    )
    op.execute("CREATE INDEX ix_geonames_places_folded ON geonames_places (folded text_pattern_ops)")
    op.execute("CREATE INDEX ix_geonames_places_ascii_name ON geonames_places (lower(ascii_name) text_pattern_ops)")
    op.create_table(
        "geonames_admin1",
        sa.Column("code", sa.Text(), primary_key=True),  # country.admin1, e.g. "TT.05"
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("folded", sa.Text(), nullable=False),
    )
    op.create_table(
        "geonames_countries",
        sa.Column("iso", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("folded", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("geonames_countries")
    op.drop_table("geonames_admin1")
    op.execute("DROP INDEX IF EXISTS ix_geonames_places_ascii_name")
    op.execute("DROP INDEX IF EXISTS ix_geonames_places_folded")
    op.drop_table("geonames_places")
    op.drop_table("plugin_data")
