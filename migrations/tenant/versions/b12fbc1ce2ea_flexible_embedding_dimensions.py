"""Make embedding_vector column dimensionless to support multiple models.

Drops all existing embedding data and rebuilds the column and index
without a fixed dimension constraint. This allows both 512-dim CLIP
and 768-dim Apple Vision feature prints (and any future model) to
coexist in the same table.

Revision ID: b12fbc1ce2ea
Revises: z3a4b5c6d7e8
Create Date: 2026-04-07
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b12fbc1ce2ea"
down_revision: Union[str, Sequence[str], None] = "z3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop all embedding data — user will regenerate
    op.execute(sa.text("TRUNCATE asset_embeddings"))

    # Drop the HNSW index (dimension-specific)
    op.drop_index("ix_asset_embeddings_hnsw", table_name="asset_embeddings", if_exists=True)

    # Alter column to dimensionless vector
    op.execute(
        sa.text("ALTER TABLE asset_embeddings ALTER COLUMN embedding_vector TYPE vector")
    )

    # Recreate HNSW index without dimension constraint.
    # pgvector 0.7+ supports dimensionless HNSW if all vectors in the index
    # have the same dimension. Since we filter by model_id in queries, we
    # create partial indexes per known model for optimal performance.
    # For now, a single index works because pgvector handles mixed dimensions
    # at query time (it just can't use the index for cross-dimension queries).
    ctx = op.get_context()
    with ctx.autocommit_block():
        op.execute(
            sa.text(
                """
                CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_asset_embeddings_hnsw
                ON asset_embeddings
                USING hnsw (embedding_vector vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                """
            )
        )


def downgrade() -> None:
    from pgvector.sqlalchemy import Vector

    # Drop all data (can't fit mixed dimensions back into Vector(512))
    op.execute(sa.text("TRUNCATE asset_embeddings"))
    op.drop_index("ix_asset_embeddings_hnsw", table_name="asset_embeddings", if_exists=True)
    op.execute(
        sa.text("ALTER TABLE asset_embeddings ALTER COLUMN embedding_vector TYPE vector(512)")
    )
    ctx = op.get_context()
    with ctx.autocommit_block():
        op.execute(
            sa.text(
                """
                CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_asset_embeddings_hnsw
                ON asset_embeddings
                USING hnsw (embedding_vector vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                """
            )
        )
