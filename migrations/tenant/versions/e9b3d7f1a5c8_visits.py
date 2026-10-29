"""Visits (ADR-025): what a public page's visitors do, counted without keeping who they are.

Revision ID: e9b3d7f1a5c8
Revises: c4e8a2d6f0b3
Create Date: 2026-10-29

visits_today: a row a visitor (a hash keyed with the day's salt), place, action and clip, while the day lasts.
visit_days and visit_sources: every day before today, totals only, added up by upkeep, which then deletes that
day's rows and its salt (visit_salts). src/server/visits.py has the rules, src/server/repository/visits.py the SQL.
Going back down drops them: nothing else reads them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9b3d7f1a5c8"
down_revision: str | Sequence[str] | None = "c4e8a2d6f0b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE visit_salts (
            day date PRIMARY KEY,
            salt bytea NOT NULL
        )
    """))
    op.execute(sa.text("""
        CREATE TABLE visits_today (
            day date NOT NULL,
            place text NOT NULL CHECK (place IN ('project', 'library', 'account')),
            place_id text NOT NULL,
            action text NOT NULL,
            clip_id text NOT NULL DEFAULT '',
            visitor text NOT NULL,
            times integer NOT NULL DEFAULT 1,
            last_at timestamptz NOT NULL,
            came_from text NOT NULL DEFAULT '',
            PRIMARY KEY (day, place, place_id, action, clip_id, visitor)
        )
    """))
    op.execute(sa.text("""
        CREATE TABLE visit_days (
            day date NOT NULL,
            place text NOT NULL,
            place_id text NOT NULL,
            action text NOT NULL,
            clip_id text NOT NULL DEFAULT '',
            visitors integer NOT NULL DEFAULT 0,
            times integer NOT NULL DEFAULT 0,
            bots integer NOT NULL DEFAULT 0,
            PRIMARY KEY (day, place, place_id, action, clip_id)
        )
    """))
    op.execute(sa.text("""
        CREATE TABLE visit_sources (
            day date NOT NULL,
            place text NOT NULL,
            place_id text NOT NULL,
            came_from text NOT NULL,
            visitors integer NOT NULL DEFAULT 0,
            PRIMARY KEY (day, place, place_id, came_from)
        )
    """))
    # A place's report reads its days in order.
    op.execute(sa.text("CREATE INDEX visit_days_place ON visit_days (place, place_id, day)"))
    op.execute(sa.text("CREATE INDEX visits_today_place ON visits_today (place, place_id)"))


def downgrade() -> None:
    for table in ("visit_sources", "visit_days", "visits_today", "visit_salts"):
        op.execute(sa.text(f"DROP TABLE {table}"))
