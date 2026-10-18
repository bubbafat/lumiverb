"""Roles per library (ADR-018 decision 8, phase 8).

Revision ID: a3c7e1f5b9d2
Revises: f2b6d8a4c1e9
Create Date: 2026-10-17

- library_roles: a person's role on one library, where it isn't the one
  their account role gives them: "admin" (the library's admin: its
  settings, its reader, its people), "editor", "viewer", or "none" (they
  don't see it). One row a person a library; it goes with the library.
  user_id is the control database's users.user_id (another database: no
  foreign key; a person's rows go when they do, routers/users.py).
- libraries.private: only the account's admins and the people given a role
  on it see it; everyone else's role on it is "none" until they're given
  one.

Nothing is set by this migration: everyone keeps the role they have on
every library (src/server/repository/library_roles.py).

Down: both go.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c7e1f5b9d2"
down_revision: str | Sequence[str] | None = "f2b6d8a4c1e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "library_roles",
        sa.Column("library_id", sa.Text(), sa.ForeignKey("libraries.library_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("user_id", sa.Text(), primary_key=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=True),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("role IN ('admin', 'editor', 'viewer', 'none')", name="library_roles_role"),
    )
    op.create_index("ix_library_roles_user_id", "library_roles", ["user_id"])
    op.add_column("libraries", sa.Column("private", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("libraries", "private")
    op.drop_index("ix_library_roles_user_id", table_name="library_roles")
    op.drop_table("library_roles")
