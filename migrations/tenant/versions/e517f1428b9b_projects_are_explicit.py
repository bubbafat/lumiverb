"""Projects are explicit lists (Robert, Oct 9).

"Smart" projects (a saved query) go: a search is saved as a search, and
saving it as a project puts the clips it matches then in an explicit list.
Smart projects are dropped, not converted (a fresh start follows).

Revision ID: e517f1428b9b
Revises: b77f2a03eee2
Create Date: 2026-10-09
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e517f1428b9b"
down_revision: Union[str, Sequence[str], None] = "b77f2a03eee2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM project_assets WHERE project_id IN (SELECT project_id FROM projects WHERE type = 'smart')")
    op.execute("DELETE FROM projects WHERE type = 'smart'")
    op.drop_column("projects", "saved_query")
    op.drop_column("projects", "type")


def downgrade() -> None:
    op.add_column("projects", sa.Column("type", sa.String(), nullable=False, server_default="static"))
    op.add_column("projects", sa.Column("saved_query", sa.JSON(), nullable=True))
