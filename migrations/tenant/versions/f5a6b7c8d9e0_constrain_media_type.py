"""Constrain media_type to 'image' or 'video'

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-03-27 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE assets
        ADD CONSTRAINT ck_assets_media_type
        CHECK (media_type IN ('image', 'video'))
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE assets
        DROP CONSTRAINT ck_assets_media_type
    """))
