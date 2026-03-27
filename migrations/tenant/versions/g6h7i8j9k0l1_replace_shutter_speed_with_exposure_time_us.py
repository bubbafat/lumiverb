"""Replace shutter_speed text with exposure_time_us bigint

Revision ID: g6h7i8j9k0l1
Revises: f5a6b7c8d9e0
Create Date: 2026-03-27 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "g6h7i8j9k0l1"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None

_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    # Drop view so we can modify columns
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))

    # Add new column
    op.add_column("assets", sa.Column("exposure_time_us", sa.BigInteger(), nullable=True))

    # Backfill from shutter_speed string or EXIF JSON ExposureTime
    # shutter_speed may be fractional ("1/250"), decimal ("0.004"), or seconds ("30")
    op.execute(sa.text("""
        UPDATE assets SET exposure_time_us = CASE
            -- Fractional: "1/250" -> parse numerator/denominator
            WHEN shutter_speed LIKE '1/%' THEN
                ROUND(1000000.0 / NULLIF(SUBSTRING(shutter_speed FROM 3)::numeric, 0))::bigint
            -- Seconds with suffix: "30s" -> strip s, multiply
            WHEN shutter_speed LIKE '%s' THEN
                ROUND(REPLACE(shutter_speed, 's', '')::numeric * 1000000)::bigint
            -- Plain decimal: "0.004" -> multiply
            WHEN shutter_speed IS NOT NULL AND shutter_speed != '' THEN
                ROUND(shutter_speed::numeric * 1000000)::bigint
            -- Fallback: try EXIF JSON
            WHEN exif IS NOT NULL AND exif::text != 'null' AND exif->>'ExposureTime' IS NOT NULL THEN
                CASE
                    WHEN exif->>'ExposureTime' LIKE '1/%' THEN
                        ROUND(1000000.0 / NULLIF(SUBSTRING(exif->>'ExposureTime' FROM 3)::numeric, 0))::bigint
                    ELSE
                        ROUND((exif->>'ExposureTime')::numeric * 1000000)::bigint
                END
            ELSE NULL
        END
        WHERE shutter_speed IS NOT NULL
           OR (exif IS NOT NULL AND exif::text != 'null' AND exif->>'ExposureTime' IS NOT NULL)
    """))

    # Index for range filtering
    op.create_index("ix_assets_exposure_time_us", "assets", ["exposure_time_us"])

    # Drop old column
    op.drop_column("assets", "shutter_speed")

    # Recreate view
    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))

    # Restore shutter_speed text column
    op.add_column("assets", sa.Column("shutter_speed", sa.Text(), nullable=True))

    # Best-effort backfill: convert microseconds back to display string
    op.execute(sa.text("""
        UPDATE assets SET shutter_speed = CASE
            WHEN exposure_time_us >= 1000000 THEN
                (exposure_time_us / 1000000)::text || 's'
            WHEN exposure_time_us > 0 THEN
                '1/' || ROUND(1000000.0 / exposure_time_us)::text
            ELSE NULL
        END
        WHERE exposure_time_us IS NOT NULL
    """))

    op.drop_index("ix_assets_exposure_time_us", table_name="assets")
    op.drop_column("assets", "exposure_time_us")

    op.execute(sa.text(_VIEW_DDL))
