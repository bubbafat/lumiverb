"""faces.embedding_model: which model embedded each face (ADR-016 phase 3, piece 4).

Revision ID: b799bec833a5
Revises: e1a2b3c4d5f6
Create Date: 2026-10-08

Re-detection pairs new faces with old ones by box overlap and embedding
distance. Embeddings from different models live in different spaces, so
they're compared only within one model; across a face-model switch, names
are re-anchored by overlap alone. Every face so far was embedded by
InsightFace's buffalo_l ArcFace (the macOS app converts the same model).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b799bec833a5"
down_revision: Union[str, Sequence[str], None] = "e1a2b3c4d5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("faces", sa.Column("embedding_model", sa.Text(), nullable=False, server_default="buffalo_l"))
    # A person's centroid averages one model's embeddings: which one.
    op.add_column("people", sa.Column("centroid_model", sa.Text(), nullable=False, server_default="buffalo_l"))


def downgrade() -> None:
    op.drop_column("people", "centroid_model")
    op.drop_column("faces", "embedding_model")
