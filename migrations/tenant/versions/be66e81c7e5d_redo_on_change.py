"""Redo on change: a settings change is the approval (ADR-016 phase 4, Robert Oct 9).

Revision ID: be66e81c7e5d
Revises: fbd7f3be1cb0
Create Date: 2026-10-09

Changing a model or setting now redoes everything made another way, after
anything missing; an admin can stop that per producer and resume it
(producer_redo_paused). The upgrade step goes: its approvals and their
clips (producer_upgrades, producer_upgrade_items), and correction_history,
which kept the edits "Replace my edits" took away (the AI never overwrites
what a person wrote now).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "be66e81c7e5d"
down_revision: Union[str, Sequence[str], None] = "fbd7f3be1cb0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "producer_redo_paused",
        sa.Column("artifact", sa.String(), primary_key=True),
        sa.Column("paused_by", sa.Text(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.drop_table("correction_history")
    op.drop_table("producer_upgrade_items")
    op.drop_table("producer_upgrades")


def downgrade() -> None:
    op.create_table(
        "producer_upgrades",
        sa.Column("upgrade_id", sa.String(), primary_key=True),
        sa.Column("artifact", sa.String(), nullable=False),
        sa.Column("producer", sa.String(), nullable=False),
        sa.Column("producer_version", sa.String(), nullable=False),
        sa.Column("settings_hash", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("library_id", sa.String(), sa.ForeignKey("libraries.library_id", ondelete="CASCADE"), nullable=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=True),
        sa.Column("edits", sa.String(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('all', 'library', 'project')", name="ck_producer_upgrades_scope"),
        sa.CheckConstraint("edits IN ('keep', 'replace', 'skip')", name="ck_producer_upgrades_edits"),
    )
    op.create_index("ix_producer_upgrades_artifact", "producer_upgrades", ["artifact"])
    op.execute("CREATE UNIQUE INDEX uq_producer_upgrades_scope ON producer_upgrades"
               " (artifact, scope, COALESCE(library_id, ''), COALESCE(project_id, ''))")
    op.create_table(
        "producer_upgrade_items",
        sa.Column("upgrade_id", sa.String(),
                  sa.ForeignKey("producer_upgrades.upgrade_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_producer_upgrade_items_asset", "producer_upgrade_items", ["asset_id"])
    op.create_table(
        "correction_history",
        sa.Column("history_id", sa.String(), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("edited_by", sa.Text(), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("replaced_by", sa.Text(), nullable=True),
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_correction_history_asset", "correction_history", ["asset_id"])
    op.drop_table("producer_redo_paused")
