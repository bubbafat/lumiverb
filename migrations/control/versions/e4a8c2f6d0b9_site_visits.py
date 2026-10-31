"""Each day's site visits, kept beyond nginx's 14 days (visitor stats in the product, Robert 2026-10-31).

Revision ID: e4a8c2f6d0b9
Revises: c5e9a3f7b1d4
Create Date: 2026-10-31

The install's, not an account's: who other than the people running this server looked at its sites (the web app
and deploy-site.sh's), from Lumiverb's copy of their nginx log (src/server/lifecycle/site_visits.py), added up a day
at a time by upkeep. Counts and likely people alone: never an address.

- site_visit_days: a day added up (once: a day there is is done), each site's browser-like addresses (browsers,
  {site: n}) and where they came from (sources, {host: n}).
- site_visit_people: that day's likely people, each what they did (why), their sites and their device, and when
  they were first seen.

Down: both tables go, with what they kept (nginx's log keeps its last 14 days).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "e4a8c2f6d0b9"
down_revision: str | Sequence[str] | None = "c5e9a3f7b1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "site_visit_days",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("browsers", JSONB(), nullable=False),
        sa.Column("sources", JSONB(), nullable=False),
        sa.Column("kept_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "site_visit_people",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("day", sa.Date(), sa.ForeignKey("site_visit_days.day", ondelete="CASCADE"), nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sites", JSONB(), nullable=False),
        sa.Column("device", sa.Text(), nullable=False),
        sa.Column("why", JSONB(), nullable=False),
    )
    op.create_index("site_visit_people_day", "site_visit_people", ["day", "first_at"])


def downgrade() -> None:
    op.drop_table("site_visit_people")
    op.drop_table("site_visit_days")
