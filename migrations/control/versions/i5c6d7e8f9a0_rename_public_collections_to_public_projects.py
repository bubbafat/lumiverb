"""Rename public_collections to public_projects (ADR-016 phase 1).

Revision ID: i5c6d7e8f9a0
Revises: h4b5c6d7e8f9
Create Date: 2026-10-07
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "i5c6d7e8f9a0"
down_revision: Union[str, Sequence[str], None] = "h4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("public_collections", "public_projects")
    op.alter_column("public_projects", "collection_id", new_column_name="project_id")


def downgrade() -> None:
    op.alter_column("public_projects", "project_id", new_column_name="collection_id")
    op.rename_table("public_projects", "public_collections")
