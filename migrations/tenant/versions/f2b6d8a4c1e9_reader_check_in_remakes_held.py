"""A reader's check-in says why its remakes wait (ADR-018 decision 6, phase 7).

Revision ID: f2b6d8a4c1e9
Revises: e9a4c2f7b1d3
Create Date: 2026-10-17

- reader_check_ins.remakes_held: why the reader holds its remakes back now,
  for each library it checks in for: "battery" (it waits for mains power) or
  "metered" (for a network that isn't metered); null when they go on. A
  device's own, as only it knows (Processing and the Admin page say it).

Down: the column goes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b6d8a4c1e9"
down_revision: str | Sequence[str] | None = "e9a4c2f7b1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reader_check_ins", sa.Column("remakes_held", sa.Text(), nullable=True))
    op.create_check_constraint("reader_check_ins_remakes_held", "reader_check_ins",
                               "remakes_held IN ('battery', 'metered')")


def downgrade() -> None:
    op.drop_constraint("reader_check_ins_remakes_held", "reader_check_ins", type_="check")
    op.drop_column("reader_check_ins", "remakes_held")
