"""Add revoked_tokens table for JWT revocation.

Revision ID: h4b5c6d7e8f9
Revises: g3a4b5c6d7e8
Create Date: 2026-03-30

Security hardening: enables server-side JWT revocation on logout and token refresh.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = "h4b5c6d7e8f9"
down_revision: Union[str, None] = "g3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "revoked_tokens",
        sa.Column("jti", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("jti"),
    )
    # Index for cleanup of old entries
    op.create_index("ix_revoked_tokens_revoked_at", "revoked_tokens", ["revoked_at"])


def downgrade() -> None:
    op.drop_index("ix_revoked_tokens_revoked_at", table_name="revoked_tokens")
    op.drop_table("revoked_tokens")
