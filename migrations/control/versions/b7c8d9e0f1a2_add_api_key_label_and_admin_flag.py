"""add_api_key_label_and_admin_flag

Revision ID: b7c8d9e0f1a2
Revises: a2b3c4d5e6f7
Create Date: 2026-03-17 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("label", sa.Text(), nullable=True))
    op.add_column(
        "api_keys",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
    )

    # Backfill: existing single key per tenant becomes admin.
    op.execute(sa.text("UPDATE api_keys SET is_admin = TRUE"))


def downgrade() -> None:
    op.drop_column("api_keys", "is_admin")
    op.drop_column("api_keys", "label")

