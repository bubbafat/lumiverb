"""Site visits kept in the control database, taken away again (Robert 2026-10-31: who looks at Lumiverb's own sites
is the people running them's, not something every install keeps).

Revision ID: a1f5d9c3e7b2
Revises: e4a8c2f6d0b9
Create Date: 2026-10-31

e4a8c2f6d0b9 made site_visit_days and site_visit_people for upkeep to fill from a Linux server's copy of its sites'
log; no release a Linux server installed kept any (v0.1.166 alone made them, and a Mac keeps no such log), so both
are empty wherever they are. `lumiverb-server site-visits` still reads nginx's own log when someone asks.

Down: both made again, empty.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "a1f5d9c3e7b2"
down_revision: str | Sequence[str] | None = "e4a8c2f6d0b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("site_visit_people")
    op.drop_table("site_visit_days")


def downgrade() -> None:
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
