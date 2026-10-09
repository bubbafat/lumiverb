"""Failures you can see (ADR-016 phase 4, Robert Oct 9).

A clip that keeps failing is given up after 10 tries (about two days with
the back-off); Settings → Processing and the CLI list failing clips with
their errors and can try them again. artifact_lineage.failed_at says when
the last try failed; a partial index finds the failing ones.

Revision ID: 5fe827012184
Revises: be66e81c7e5d
Create Date: 2026-10-09
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "5fe827012184"
down_revision: Union[str, Sequence[str], None] = "be66e81c7e5d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifact_lineage", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("CREATE INDEX ix_artifact_lineage_failing ON artifact_lineage (artifact, failed_at DESC)"
               " WHERE error IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_artifact_lineage_failing")
    op.drop_column("artifact_lineage", "failed_at")
