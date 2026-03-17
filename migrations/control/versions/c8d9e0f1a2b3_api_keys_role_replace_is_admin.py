"""api_keys role replace is_admin

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-03-17 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("role", sa.Text(), nullable=False, server_default="member"),
    )
    op.execute(sa.text("UPDATE api_keys SET role = 'admin' WHERE is_admin = TRUE"))
    op.drop_column("api_keys", "is_admin")


def downgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
    )
    op.execute(sa.text("UPDATE api_keys SET is_admin = TRUE WHERE role = 'admin'"))
    op.drop_column("api_keys", "role")
