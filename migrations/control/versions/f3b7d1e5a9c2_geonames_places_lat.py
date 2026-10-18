"""The nearest town to a point, quickly (ADR-021, the Place names plug-in's producer).

Revision ID: f3b7d1e5a9c2
Revises: e2a6c4f8b0d1
Create Date: 2026-10-18

geonames_places by latitude: the nearest-town lookup
(src/server/repository/places.py nearest) reads a band of latitude around
the point, then measures. A section of a town (PPLX, "Zürich (Kreis 7)")
is never a town's name, so it's left out of the index too.

Down: the index goes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f3b7d1e5a9c2"
down_revision: str | Sequence[str] | None = "e2a6c4f8b0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE INDEX ix_geonames_places_lat ON geonames_places (lat)"
               " WHERE feature_code NOT IN ('PPLX', 'PPLH', 'PPLQ', 'PPLW', 'PPLCH')")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_geonames_places_lat")
