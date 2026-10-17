"""Keys that go unused end on their own: an account's key-idle days.

Revision ID: c9e5a7b3d1f4
Revises: a7c3e5f9b1d2
Create Date: 2026-10-17

tenants.key_idle_days: how many days a key a person made may go unused
before upkeep ends it (ApiKeyRepository.end_unused); NULL, every account's
until an admin sets it, keeps keys until someone revokes them. Beside the
keys it ends, in the control database, so listing keys and ending them read
nothing else.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e5a7b3d1f4"
down_revision: str | Sequence[str] | None = "a7c3e5f9b1d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("key_idle_days", sa.Integer(), nullable=True))
    op.create_check_constraint("tenants_key_idle_days", "tenants", "key_idle_days BETWEEN 1 AND 3650")


def downgrade() -> None:
    op.drop_constraint("tenants_key_idle_days", "tenants", type_="check")
    op.drop_column("tenants", "key_idle_days")
