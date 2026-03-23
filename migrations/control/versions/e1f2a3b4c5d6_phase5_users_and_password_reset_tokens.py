"""Phase 5 auth: users table, password_reset_tokens table, api_keys.role default to admin

Revision ID: e1f2a3b4c5d6
Revises: d9e0f1a2b3c4
Create Date: 2026-03-23 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), sa.ForeignKey("tenants.tenant_id"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="viewer"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "password_reset_tokens",
        sa.Column("token_hash", sa.Text(), primary_key=True),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )

    # All API keys are operator-controlled and should be admin.
    # Backfill any rows still set to the old "member" default, then change the column default.
    op.execute(sa.text("UPDATE api_keys SET role = 'admin' WHERE role = 'member'"))
    op.alter_column(
        "api_keys",
        "role",
        existing_type=sa.Text(),
        existing_nullable=False,
        server_default="admin",
    )


def downgrade() -> None:
    # NOTE: The member→admin backfill in upgrade() is intentionally non-reversible.
    # The 'member' role is being removed from api_keys vocabulary in Phase 5; all API
    # keys are operator-controlled and must be admin. Restoring rows to 'member' would
    # produce broken state. Only the column default is rolled back here.
    op.alter_column(
        "api_keys",
        "role",
        existing_type=sa.Text(),
        existing_nullable=False,
        server_default="member",
    )
    op.drop_table("password_reset_tokens")
    op.drop_table("users")
