"""add_scans_table

Revision ID: a1b2c3d4e5f6
Revises: d2ebbb76fc6f
Create Date: 2026-03-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "d2ebbb76fc6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add scans table and related columns."""
    # Add columns to libraries
    op.add_column(
        "libraries",
        sa.Column("last_scan_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "libraries",
        sa.Column("last_scan_error", sa.String(), nullable=True),
    )

    # Create scans table (before assets.last_scan_id FK)
    op.create_table(
        "scans",
        sa.Column("scan_id", sa.String(), nullable=False),
        sa.Column("library_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("root_path_override", sa.String(), nullable=True),
        sa.Column("worker_id", sa.String(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("files_discovered", sa.Integer(), nullable=True),
        sa.Column("files_added", sa.Integer(), nullable=True),
        sa.Column("files_updated", sa.Integer(), nullable=True),
        sa.Column("files_missing", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["library_id"], ["libraries.library_id"]),
        sa.PrimaryKeyConstraint("scan_id"),
    )

    # Add last_scan_id to assets (FK to scans)
    op.add_column(
        "assets",
        sa.Column("last_scan_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_assets_last_scan_id_scans",
        "assets",
        "scans",
        ["last_scan_id"],
        ["scan_id"],
    )


def downgrade() -> None:
    """Remove scans table and related columns (reverse FK order)."""
    op.drop_constraint("fk_assets_last_scan_id_scans", "assets", type_="foreignkey")
    op.drop_column("assets", "last_scan_id")

    op.drop_table("scans")

    op.drop_column("libraries", "last_scan_error")
    op.drop_column("libraries", "last_scan_at")
