"""The built-in machine finds faces (ADR-022 phase 3).

Revision ID: d1f5b9c3e7a2
Revises: c5e9a3b7d1f4
Create Date: 2026-10-18

Face detection (photos' and videos' frames) became an AI job (`faces`): the
built-in machine, the scheduler's own computer, does it as the scheduler
always has, so every account's built-in machine is given it (a new
account's has it from the start: BUILT_IN_JOBS). Its model is the job's
default (buffalo_l, the producers' `model` setting), so nothing is found
again.

Down: the built-in machines' `faces` goes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d1f5b9c3e7a2"
down_revision: str | Sequence[str] | None = "c5e9a3b7d1f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE ai_machines SET jobs = jobs || '[\"faces\"]'::jsonb"
               " WHERE built_in AND NOT jobs @> '[\"faces\"]'::jsonb")


def downgrade() -> None:
    op.execute("UPDATE ai_machines SET jobs = jobs - 'faces' WHERE built_in")
