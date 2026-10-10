"""Who minted each API key, so a key doesn't outlive its creator's role.

Revision ID: 7c2d4e6f8a1b
Revises: 6ab1e1dd3ef7
Create Date: 2026-10-09

api_keys.created_by_user_id: the signed-in user who made the key (NULL for
keys an admin or operator minted outside a user, like the scheduler's).
Demoting a user revokes their keys above the new role; deleting one revokes
all of them.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7c2d4e6f8a1b"
down_revision: Union[str, Sequence[str], None] = "6ab1e1dd3ef7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("created_by_user_id", sa.String(), nullable=True))
    op.create_index("ix_api_keys_created_by_user_id", "api_keys", ["created_by_user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_created_by_user_id", table_name="api_keys")
    op.drop_column("api_keys", "created_by_user_id")
