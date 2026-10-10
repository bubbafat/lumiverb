"""One pause table: each switch with its scope (ADR-016 phase 4).

Revision ID: 4e3bcb0d9be8
Revises: a7c3e9d1f2b4
Create Date: 2026-10-09

processing_paused (a switch paused: nothing of it starts) and
producer_redo_paused (a producer's redo stopped: its stale clips wait)
become producer_pauses, a row per (target, scope): scope 'work' is all of
a switch's work (Scans, Upkeep or a producer's, missing and stale), 'redo'
a producer's redo alone. The scheduler reads both in one go. Every row is
carried over, with who paused it and when (a new version's stop, by no
one, stays by no one).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "4e3bcb0d9be8"
down_revision: Union[str, Sequence[str], None] = "a7c3e9d1f2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "producer_pauses",
        sa.Column("target", sa.String(), primary_key=True),
        sa.Column("scope", sa.String(), primary_key=True),
        sa.Column("paused_by", sa.Text(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('work', 'redo')", name="ck_producer_pauses_scope"),
    )
    op.execute(sa.text("INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
                       " SELECT target, 'work', paused_by, paused_at FROM processing_paused"))
    op.execute(sa.text("INSERT INTO producer_pauses (target, scope, paused_by, paused_at)"
                       " SELECT artifact, 'redo', paused_by, paused_at FROM producer_redo_paused"))
    op.drop_table("processing_paused")
    op.drop_table("producer_redo_paused")


def downgrade() -> None:
    op.create_table(
        "processing_paused",
        sa.Column("target", sa.String(), primary_key=True),
        sa.Column("paused_by", sa.Text(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "producer_redo_paused",
        sa.Column("artifact", sa.String(), primary_key=True),
        sa.Column("paused_by", sa.Text(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(sa.text("INSERT INTO processing_paused (target, paused_by, paused_at)"
                       " SELECT target, paused_by, paused_at FROM producer_pauses WHERE scope = 'work'"))
    op.execute(sa.text("INSERT INTO producer_redo_paused (artifact, paused_by, paused_at)"
                       " SELECT target, paused_by, paused_at FROM producer_pauses WHERE scope = 'redo'"))
    op.drop_table("producer_pauses")
