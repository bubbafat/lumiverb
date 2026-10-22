"""Each account's folders on this server, the server admin's to give.

Revision ID: b3f7d1a9c5e8
Revises: e8b4c2f6a0d3
Create Date: 2026-10-22

tenants.folders: the folders a library this server reads may be in for the
account (a JSON list, in the form library roots are kept), set by a server
admin (PUT /v1/admin/tenants/{id}/folders). NULL, every account's after
this, is every folder the server reads, as before: nothing changes until a
server admin gives an account folders of its own.

Down: the column goes, and every account may choose any folder the server
reads again.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b3f7d1a9c5e8"
down_revision: str | Sequence[str] | None = "e8b4c2f6a0d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("folders", JSONB(), nullable=True))
    op.create_check_constraint("tenants_folders", "tenants", "folders IS NULL OR jsonb_typeof(folders) = 'array'")


def downgrade() -> None:
    op.drop_constraint("tenants_folders", "tenants", type_="check")
    op.drop_column("tenants", "folders")
