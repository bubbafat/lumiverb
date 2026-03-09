"""add_asset_exif_columns

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("exif", sa.JSON(), nullable=True))
    op.add_column(
        "assets",
        sa.Column("exif_extracted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("assets", sa.Column("camera_make", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("camera_model", sa.Text(), nullable=True))
    op.add_column(
        "assets",
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("assets", sa.Column("gps_lat", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("gps_lon", sa.Float(), nullable=True))


def downgrade() -> None:
    for col in (
        "exif",
        "exif_extracted_at",
        "camera_make",
        "camera_model",
        "taken_at",
        "gps_lat",
        "gps_lon",
    ):
        op.drop_column("assets", col)
