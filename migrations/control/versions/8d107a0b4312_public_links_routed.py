"""Public links find their account's database from its routing.

Revision ID: 8d107a0b4312
Revises: 7c2d4e6f8a1b
Create Date: 2026-10-13

public_libraries and public_projects kept a copy of the account's database
address, made when the link was, which a restore never rewrote; the
middleware now reads tenant_db_routing for them as for every request, so
the copies go. Going back puts them back from the routing.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8d107a0b4312"
down_revision: Union[str, Sequence[str], None] = "7c2d4e6f8a1b"
branch_labels = None
depends_on = None

TABLES = ("public_libraries", "public_projects")


def upgrade() -> None:
    for table in TABLES:
        op.drop_column(table, "connection_string")


def downgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("connection_string", sa.Text(), nullable=True))
        op.execute(
            f"UPDATE {table} p SET connection_string = r.connection_string"
            " FROM tenant_db_routing r WHERE r.tenant_id = p.tenant_id"
        )
        # A link whose account has no routing reached nothing before either.
        op.execute(f"DELETE FROM {table} WHERE connection_string IS NULL")
        op.alter_column(table, "connection_string", nullable=False)
