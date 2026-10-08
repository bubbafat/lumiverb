"""AI machines: the account's GPU machines, each with the jobs it does (ADR-016 phase 3).

The one vision endpoint a tenant had becomes its first machine, doing
descriptions & text, two requests at once (what the worker's
vision_concurrency defaulted to). The vision model stays on the tenant:
one model per job.

Revision ID: 8dfad06a4714
Revises: i5c6d7e8f9a0
Create Date: 2026-10-08
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8dfad06a4714"
down_revision: Union[str, Sequence[str], None] = "i5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_machines",
        sa.Column("machine_id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("api_url", sa.Text(), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("jobs", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("at_once", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # The latest check (the worker's, or Connect's when saved): NULL until there is one.
        sa.Column("online", sa.Boolean(), nullable=True),
        sa.Column("status_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("models", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "name", name="uq_ai_machines_tenant_name"),
    )
    op.create_index("ix_ai_machines_tenant_id", "ai_machines", ["tenant_id"])
    op.execute(sa.text(
        "INSERT INTO ai_machines (machine_id, tenant_id, name, api_url, api_key, jobs, at_once)"
        " SELECT 'aim_' || substr(md5(tenant_id), 1, 20), tenant_id,"
        "   COALESCE(NULLIF(substring(vision_api_url from '://([^/]+)'), ''), 'Vision'),"
        "   rtrim(vision_api_url, '/'), vision_api_key, '[\"vision\"]'::jsonb, 2"
        " FROM tenants WHERE vision_api_url <> ''"
    ))
    op.drop_column("tenants", "vision_api_url")
    op.drop_column("tenants", "vision_api_key")


def downgrade() -> None:
    op.add_column("tenants", sa.Column("vision_api_url", sa.String(), nullable=False, server_default=""))
    op.add_column("tenants", sa.Column("vision_api_key", sa.String(), nullable=False, server_default=""))
    op.execute(sa.text(
        "UPDATE tenants t SET vision_api_url = m.api_url, vision_api_key = m.api_key"
        " FROM (SELECT DISTINCT ON (tenant_id) tenant_id, api_url, api_key FROM ai_machines"
        "       WHERE enabled AND jobs ? 'vision' ORDER BY tenant_id, created_at, machine_id) m"
        " WHERE m.tenant_id = t.tenant_id"
    ))
    op.drop_index("ix_ai_machines_tenant_id", table_name="ai_machines")
    op.drop_table("ai_machines")
