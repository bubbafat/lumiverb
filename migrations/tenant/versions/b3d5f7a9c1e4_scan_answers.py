"""A scan keeps what it answered, so a call sent again gets the same answer.

Revision ID: b3d5f7a9c1e4
Revises: e7b9d1f3a5c2
Create Date: 2026-10-13

- scan_sessions.answers: what the scan answered, by call: the plan's counts,
  the answer to its moves, and its finish. A client whose answer was lost
  (a dropped connection, the API restarting) sends the call again and gets
  the answer the first got: a finish sent again is no longer a 409.

Down: drops the column (a call sent again is a 409 again, as before).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b3d5f7a9c1e4"
down_revision: str | Sequence[str] | None = "e7b9d1f3a5c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scan_sessions", sa.Column("answers", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("scan_sessions", "answers")
