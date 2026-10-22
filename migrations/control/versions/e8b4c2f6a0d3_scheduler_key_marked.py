"""The scheduler's own key is known by a mark only it gets.

Revision ID: e8b4c2f6a0d3
Revises: d1f5a9c3e7b2
Create Date: 2026-10-22

The one key an AI machine's key is handed to (repository/ai_machines.py
key_for) was known by its label, "scheduler", and by no person or device
having made it. api_keys.scheduler marks it instead, set only where the
scheduler makes its key (ApiKeyRepository.new_scheduler_key), and only on
a key made by no person and no device's (api_keys_scheduler). The keys
today's rule takes for the scheduler's are marked; the scheduler's next
start revokes them and makes a new one.

Down: the column goes, and the label alone says which is the scheduler's.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b4c2f6a0d3"
down_revision: str | Sequence[str] | None = "d1f5a9c3e7b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("scheduler", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("""
        UPDATE api_keys SET scheduler = true
        WHERE label = 'scheduler' AND created_by_user_id IS NULL AND device_id IS NULL
    """)
    op.create_check_constraint("api_keys_scheduler", "api_keys",
                               "NOT scheduler OR (created_by_user_id IS NULL AND device_id IS NULL)")


def downgrade() -> None:
    op.drop_constraint("api_keys_scheduler", "api_keys", type_="check")
    op.drop_column("api_keys", "scheduler")
