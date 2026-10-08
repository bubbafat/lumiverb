"""Transcripts become an AI job (ADR-016 phase 3).

Every tenant gets its built-in machine: the worker's own Whisper, with no
URL, doing transcripts one at a time, as the worker did. It's listed
first (as old as its tenant). The job's model starts at small, what every
transcript so far was made with, so none goes stale. One built-in machine
per tenant.

Revision ID: b6591b39778f
Revises: 8dfad06a4714
Create Date: 2026-10-08
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6591b39778f"
down_revision: Union[str, Sequence[str], None] = "8dfad06a4714"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("transcript_model_id", sa.String(), nullable=False, server_default="small"))
    op.add_column("ai_machines", sa.Column("built_in", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("uq_ai_machines_one_built_in", "ai_machines", ["tenant_id"], unique=True,
                    postgresql_where=sa.text("built_in"))
    op.execute(sa.text(
        "INSERT INTO ai_machines (machine_id, tenant_id, name, api_url, api_key, jobs, at_once, enabled,"
        " built_in, created_at)"
        " SELECT 'aim_' || substr(md5('built_in:' || t.tenant_id), 1, 22), t.tenant_id,"
        "   CASE WHEN EXISTS (SELECT 1 FROM ai_machines m WHERE m.tenant_id = t.tenant_id AND m.name = 'Built in')"
        "        THEN 'Built in (2)' ELSE 'Built in' END,"
        "   '', '', '[\"transcripts\"]'::jsonb, 1, true, true, t.created_at"
        " FROM tenants t"
    ))


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM ai_machines WHERE built_in"))
    op.execute(sa.text("UPDATE ai_machines SET jobs = jobs - 'transcripts' WHERE jobs ? 'transcripts'"))
    op.drop_index("uq_ai_machines_one_built_in", table_name="ai_machines")
    op.drop_column("ai_machines", "built_in")
    op.drop_column("tenants", "transcript_model_id")
