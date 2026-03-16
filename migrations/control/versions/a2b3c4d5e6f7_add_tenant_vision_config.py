"""add_tenant_vision_config

Revision ID: a2b3c4d5e6f7
Revises: 5ea9891b5c11
Create Date: 2026-03-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2b3c4d5e6f7'
down_revision: Union[str, Sequence[str], None] = '5ea9891b5c11'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tenants', sa.Column('vision_api_url', sa.String(), nullable=False, server_default=''))
    op.add_column('tenants', sa.Column('vision_api_key', sa.String(), nullable=False, server_default=''))


def downgrade() -> None:
    op.drop_column('tenants', 'vision_api_key')
    op.drop_column('tenants', 'vision_api_url')
