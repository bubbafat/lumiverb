"""Add EXIF detail columns to assets

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-03-27 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("iso", sa.Integer(), nullable=True))
    op.add_column("assets", sa.Column("shutter_speed", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("aperture", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("focal_length", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("focal_length_35mm", sa.Float(), nullable=True))
    op.add_column("assets", sa.Column("lens_model", sa.Text(), nullable=True))
    op.add_column("assets", sa.Column("flash_fired", sa.Boolean(), nullable=True))
    op.add_column("assets", sa.Column("orientation", sa.Integer(), nullable=True))

    # Indexes for filtering and sorting
    op.create_index("ix_assets_iso", "assets", ["iso"])
    op.create_index("ix_assets_aperture", "assets", ["aperture"])
    op.create_index("ix_assets_focal_length", "assets", ["focal_length"])
    op.create_index("ix_assets_lens_model", "assets", ["lens_model"])
    op.create_index("ix_assets_library_gps", "assets", ["library_id", "gps_lat", "gps_lon"])

    # Composite index for default sort query (taken_at DESC NULLS LAST, asset_id DESC)
    op.execute("""
        CREATE INDEX ix_assets_library_taken_at
        ON assets (library_id, taken_at DESC NULLS LAST, asset_id DESC)
    """)

    # Backfill from EXIF JSON blob
    op.execute("""
        UPDATE assets SET
          iso = (exif->>'ISO')::int,
          shutter_speed = exif->>'ExposureTime',
          aperture = COALESCE((exif->>'FNumber')::float, (exif->>'ApertureValue')::float),
          focal_length = (exif->>'FocalLength')::float,
          focal_length_35mm = (exif->>'FocalLengthIn35mmFormat')::float,
          lens_model = COALESCE(exif->>'LensModel', exif->>'LensID'),
          flash_fired = CASE
            WHEN exif->>'Flash' ILIKE '%fired%'
              AND exif->>'Flash' NOT ILIKE '%not fire%' THEN TRUE
            WHEN exif->>'Flash' ILIKE '%no flash%'
              OR exif->>'Flash' ILIKE '%not fire%'
              OR exif->>'Flash' ILIKE '%off%' THEN FALSE
            ELSE NULL END,
          orientation = (exif->>'Orientation')::int
        WHERE exif IS NOT NULL
          AND exif::text != 'null'
          AND exif::text != '{}'
    """)


def downgrade() -> None:
    op.drop_index("ix_assets_library_taken_at", table_name="assets")
    op.drop_index("ix_assets_library_gps", table_name="assets")
    op.drop_index("ix_assets_lens_model", table_name="assets")
    op.drop_index("ix_assets_focal_length", table_name="assets")
    op.drop_index("ix_assets_aperture", table_name="assets")
    op.drop_index("ix_assets_iso", table_name="assets")

    op.drop_column("assets", "orientation")
    op.drop_column("assets", "flash_fired")
    op.drop_column("assets", "lens_model")
    op.drop_column("assets", "focal_length_35mm")
    op.drop_column("assets", "focal_length")
    op.drop_column("assets", "aperture")
    op.drop_column("assets", "shutter_speed")
    op.drop_column("assets", "iso")
