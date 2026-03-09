"""replace_asset_metadata

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-03-09

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("asset_metadata")
    op.create_table(
        "asset_metadata",
        sa.Column("metadata_id", sa.Text(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.Text(),
            sa.ForeignKey("assets.asset_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("model_version", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_asset_metadata_asset_id", "asset_metadata", ["asset_id"])
    op.create_unique_constraint(
        "uq_asset_metadata_asset_model_version",
        "asset_metadata",
        ["asset_id", "model_id", "model_version"],
    )


def downgrade() -> None:
    op.drop_table("asset_metadata")
    op.create_table(
        "asset_metadata",
        sa.Column("asset_id", sa.Text(), primary_key=True),
        sa.Column("exif_json", postgresql.JSONB(), nullable=True),
        sa.Column("sharpness_score", sa.Float(), nullable=True),
        sa.Column("face_count", sa.Integer(), nullable=True),
        sa.Column("ai_description", sa.Text(), nullable=True),
        sa.Column("ai_tags", postgresql.JSONB(), nullable=True),
        sa.Column("ai_ocr_text", sa.Text(), nullable=True),
        sa.Column("ai_description_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
