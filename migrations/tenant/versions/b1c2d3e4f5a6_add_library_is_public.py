"""add library is_public column

Revision ID: b1c2d3e4f5a6
Revises: a0b9c8d7e6f5
Create Date: 2026-03-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a0b9c8d7e6f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
    )


def downgrade() -> None:
    op.drop_column("libraries", "is_public")
