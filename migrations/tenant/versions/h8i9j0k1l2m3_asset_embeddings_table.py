"""
Replace embedding_vector on assets with asset_embeddings table.
Add vision_model_id to libraries.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "h8i9j0k1l2m3"
down_revision: Union[str, Sequence[str], None] = "g7h8i9j0k1l2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop embedding_vector from assets (replaced by asset_embeddings table)
    op.drop_index("ix_assets_embedding_hnsw", table_name="assets", if_exists=True)
    op.drop_column("assets", "embedding_vector")

    # Add vision_model_id to libraries
    op.add_column(
        "libraries",
        sa.Column(
            "vision_model_id",
            sa.String(),
            nullable=False,
            server_default="moondream",
        ),
    )

    # Create asset_embeddings table
    op.create_table(
        "asset_embeddings",
        sa.Column("embedding_id", sa.String(), primary_key=True),
        sa.Column(
            "asset_id",
            sa.String(),
            sa.ForeignKey("assets.asset_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("model_id", sa.String(), nullable=False),
        sa.Column("model_version", sa.String(), nullable=False),
        sa.Column("embedding_vector", Vector(512), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "asset_id",
            "model_id",
            "model_version",
            name="uq_asset_embeddings_asset_model_version",
        ),
    )

    # HNSW index on asset_embeddings — must run outside a transaction (CONCURRENTLY)
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

    # Plain index on asset_id for fast per-asset lookups
    op.create_index("ix_asset_embeddings_asset_id", "asset_embeddings", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_asset_embeddings_asset_id", table_name="asset_embeddings")
    op.drop_index("ix_asset_embeddings_hnsw", table_name="asset_embeddings")
    op.drop_table("asset_embeddings")
    op.drop_column("libraries", "vision_model_id")
    op.add_column(
        "assets",
        sa.Column("embedding_vector", Vector(512), nullable=True),
    )

