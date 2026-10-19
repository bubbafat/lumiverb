"""The built-in Place names' list goes (ADR-023 phase 3, part 4: the removal).

Revision ID: d7f3b9e1c5a2
Revises: c4e8a2f6b1d9
Create Date: 2026-10-19

The GeoNames plug-in keeps its own list (src/own_plugins/geonames/) since
part 3, and nothing reads these any more:

- geonames_places, geonames_landmarks, geonames_names, geonames_admin1 and
  geonames_countries: the list ADR-021's Place names fetched into the
  control database;
- plugin_data: what one of ADR-021's plug-ins kept for the whole server,
  and how far its fetch was (GeoNames' was the only one).

Down: each is made again, empty (as a server that never turned Place names
on had them).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7f3b9e1c5a2"
down_revision: str | Sequence[str] | None = "c4e8a2f6b1d9"
branch_labels = None
depends_on = None

TABLES = ("geonames_names", "geonames_landmarks", "geonames_places", "geonames_admin1", "geonames_countries",
          "plugin_data")


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table}")


def _place(name: str, *more: sa.Column) -> None:
    op.create_table(
        name,
        sa.Column("geonameid", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ascii_name", sa.Text(), nullable=False),
        sa.Column("folded", sa.Text(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("feature_code", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=False),
        sa.Column("admin1_code", sa.Text(), nullable=False),
        *more,
    )


def downgrade() -> None:
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
        sa.Column("version", sa.Integer(), nullable=True),
        sa.CheckConstraint("state IN ('fetching', 'ready', 'failed')", name="plugin_data_state"),
    )
    _place("geonames_places", sa.Column("population", sa.BigInteger(), nullable=False),
           sa.Column("timezone", sa.Text(), nullable=False))
    op.execute("CREATE INDEX ix_geonames_places_folded ON geonames_places (folded text_pattern_ops)")
    op.execute("CREATE INDEX ix_geonames_places_ascii_name ON geonames_places (lower(ascii_name) text_pattern_ops)")
    op.execute("CREATE INDEX ix_geonames_places_lat ON geonames_places (lat)"
               " WHERE feature_code NOT IN ('PPLX', 'PPLH', 'PPLQ', 'PPLW', 'PPLCH')")
    for name in ("geonames_admin1", "geonames_countries"):
        op.create_table(
            name,
            sa.Column("code" if name == "geonames_admin1" else "iso", sa.Text(), primary_key=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("folded", sa.Text(), nullable=False),
        )
    _place("geonames_landmarks", sa.Column("known", sa.Integer(), nullable=False, server_default="0"))
    op.execute("CREATE INDEX ix_geonames_landmarks_lat ON geonames_landmarks (lat)")
    op.execute("CREATE INDEX ix_geonames_landmarks_folded ON geonames_landmarks (folded text_pattern_ops)")
    op.execute("CREATE INDEX ix_geonames_landmarks_ascii_name"
               " ON geonames_landmarks (lower(ascii_name) text_pattern_ops)")
    op.create_table(
        "geonames_names",
        sa.Column("geonameid", sa.Integer(), nullable=False),
        sa.Column("folded", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("geonameid", "folded"),
    )
    op.execute("CREATE INDEX ix_geonames_names_folded ON geonames_names (folded text_pattern_ops)")
