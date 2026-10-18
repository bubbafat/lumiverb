"""The built-in machine does CLIP (ADR-022 phase 2).

Revision ID: c5e9a3b7d1f4
Revises: b3d7f1a5c9e2
Create Date: 2026-10-18

CLIP vectors became an AI job (`clip`): the built-in machine, the
scheduler's own computer, does it as the scheduler always has, so every
account's built-in machine is given it (a new account's has it from the
start: BUILT_IN_JOBS). Its model is the job's default (the producer's,
ViT-B-32/openai), so nothing is made again.

Down: the built-in machines' `clip` goes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c5e9a3b7d1f4"
down_revision: str | Sequence[str] | None = "b3d7f1a5c9e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE ai_machines SET jobs = jobs || '[\"clip\"]'::jsonb"
               " WHERE built_in AND NOT jobs @> '[\"clip\"]'::jsonb")


def downgrade() -> None:
    op.execute("UPDATE ai_machines SET jobs = jobs - 'clip' WHERE built_in")
