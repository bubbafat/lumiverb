"""producer_upgrades: stale artifacts an admin approved making again (ADR-016 phase 3, piece 5).

Revision ID: fbd7f3be1cb0
Revises: fab90a220ba5
Create Date: 2026-10-08

An upgrade is the answer to "NNN clips were made with the old model or
settings. Upgrade them now?": the producer's settings it upgrades to, the
scope it was narrowed to, what happens to clips with a person's edits, and
the clips that were stale in that scope then (its items). The reconciler
hands an item out until it's made again after the approval.

correction_history keeps the edits "Replace my edits" took away.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "fbd7f3be1cb0"
down_revision: Union[str, Sequence[str], None] = "fab90a220ba5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "producer_upgrades",
        sa.Column("upgrade_id", sa.String(), primary_key=True),
        sa.Column("artifact", sa.String(), nullable=False),
        # What it upgrades to: the producer's lineage when it was approved.
        sa.Column("producer", sa.String(), nullable=False),
        sa.Column("producer_version", sa.String(), nullable=False),
        sa.Column("settings_hash", sa.String(), nullable=False),
        # Everything, one library or one project.
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("library_id", sa.String(), sa.ForeignKey("libraries.library_id", ondelete="CASCADE"), nullable=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=True),
        sa.Column("edits", sa.String(), nullable=False),  # keep | replace | skip
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('all', 'library', 'project')", name="ck_producer_upgrades_scope"),
        sa.CheckConstraint("edits IN ('keep', 'replace', 'skip')", name="ck_producer_upgrades_edits"),
    )
    op.create_index("ix_producer_upgrades_artifact", "producer_upgrades", ["artifact"])
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
        sa.Column("field", sa.String(), nullable=False),  # description | ocr_text | tags
        sa.Column("value", postgresql.JSONB(), nullable=False),  # the edit as it was
        sa.Column("edited_by", sa.Text(), nullable=True),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),  # e.g. "upgrade:vision"
        sa.Column("replaced_by", sa.Text(), nullable=True),
        sa.Column("replaced_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_correction_history_asset", "correction_history", ["asset_id"])


def downgrade() -> None:
    op.drop_table("correction_history")
    op.drop_table("producer_upgrade_items")
    op.drop_table("producer_upgrades")
