"""Rename collections to projects and give projects a lifecycle (ADR-016 phase 1).

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-10-07

What the code called "collections" are projects: transient, many-to-many
sets of clips for one job. "Collection" is reserved for a future
long-lived container. Rows, ids and foreign keys carry over unchanged.
Constraint names keep their old prefix (they're invisible and renaming
them could fail on a tenant whose names differ); the explicit index is
renamed.

Lifecycle: status 'active' | 'archived'. Archived projects leave the
sidebar and pickers but keep their clips and can still be exported.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "t5u6v7w8x9y0"
down_revision: Union[str, Sequence[str], None] = "s4t5u6v7w8x9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("collections", "projects")
    op.alter_column("projects", "collection_id", new_column_name="project_id")
    op.rename_table("collection_assets", "project_assets")
    op.alter_column("project_assets", "collection_id", new_column_name="project_id")
    op.execute(sa.text("ALTER INDEX ix_collection_assets_asset_id RENAME TO ix_project_assets_asset_id"))

    op.add_column(
        "projects",
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
    )
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_projects_status", "projects", "status IN ('active', 'archived')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_projects_status", "projects", type_="check")
    op.drop_column("projects", "archived_at")
    op.drop_column("projects", "status")
    op.execute(sa.text("ALTER INDEX ix_project_assets_asset_id RENAME TO ix_collection_assets_asset_id"))
    op.alter_column("project_assets", "project_id", new_column_name="collection_id")
    op.rename_table("project_assets", "collection_assets")
    op.alter_column("projects", "project_id", new_column_name="collection_id")
    op.rename_table("projects", "collections")
