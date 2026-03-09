"""Add embedding_vector to assets table."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "g7h8i9j0k1l2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "assets",
        sa.Column("embedding_vector", Vector(512), nullable=True),
    )
    # HNSW index — must run outside a transaction (CONCURRENTLY)
    ctx = op.get_context()
    with ctx.autocommit_block():
        op.execute(
            sa.text(
                """
                CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_assets_embedding_hnsw
                ON assets
                USING hnsw (embedding_vector vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                """
            )
        )


def downgrade() -> None:
    op.drop_index("ix_assets_embedding_hnsw", table_name="assets")
    op.drop_column("assets", "embedding_vector")

