"""library_path_filters and tenant_path_filter_defaults

Revision ID: m5n6o7p8q9r0
Revises: a4b5c6d7e8f9
Create Date: 2026-03-17

Adds library_path_filters (per-library include/exclude) and
tenant_path_filter_defaults (tenant-level defaults copied to new libraries).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "m5n6o7p8q9r0"
down_revision: Union[str, Sequence[str], None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "library_path_filters",
        sa.Column("filter_id", sa.String(), nullable=False),
        sa.Column("library_id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("pattern", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("type IN ('include', 'exclude')", name="library_path_filters_type_check"),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("filter_id"),
    )
    op.create_index("ix_library_path_filters_library_id", "library_path_filters", ["library_id"])

    op.create_table(
        "tenant_path_filter_defaults",
        sa.Column("default_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("pattern", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("type IN ('include', 'exclude')", name="tenant_path_filter_defaults_type_check"),
        sa.PrimaryKeyConstraint("default_id"),
    )
    op.create_index("ix_tenant_path_filter_defaults_tenant_id", "tenant_path_filter_defaults", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_tenant_path_filter_defaults_tenant_id", "tenant_path_filter_defaults")
    op.drop_table("tenant_path_filter_defaults")
    op.drop_index("ix_library_path_filters_library_id", "library_path_filters")
    op.drop_table("library_path_filters")
