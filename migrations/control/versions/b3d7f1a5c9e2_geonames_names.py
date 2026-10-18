"""Places found by their names in other languages (ADR-021's Place names, Robert 2026-10-18).

Revision ID: b3d7f1a5c9e2
Revises: a9c5e1f7b3d2
Create Date: 2026-10-18

geonames_names: the other names of the towns and landmarks kept ("Eiffel
Tower" for Tour Eiffel, "München" for Munich, "Zurigo" for Zürich), from
GeoNames' alternate names: names in a language or none, never historic or
colloquial ones nor codes (airports', postal), and never one that folds to
the place's own name, with its language (English first where several
match: the app's). Searched by name the way the places are (folded,
src/server/repository/places.py fold), by prefix.

geonames_landmarks.known: how many other names a landmark has, how known
it is (the Eiffel Tower has a hundred, Eiffel Peak none), for ranking
landmarks found by name, which have no population.

Nothing is fetched by this migration.

Down: both go.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d7f1a5c9e2"
down_revision: str | Sequence[str] | None = "a9c5e1f7b3d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "geonames_names",
        sa.Column("geonameid", sa.Integer(), nullable=False),
        sa.Column("folded", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),  # "en", "zh-Hant"; "" for none
        sa.PrimaryKeyConstraint("geonameid", "folded"),
    )
    op.execute("CREATE INDEX ix_geonames_names_folded ON geonames_names (folded text_pattern_ops)")
    op.add_column("geonames_landmarks", sa.Column("known", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("geonames_landmarks", "known")
    op.drop_table("geonames_names")
