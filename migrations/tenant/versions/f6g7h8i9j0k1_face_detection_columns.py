"""Add face detection columns and indexes.

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-03-30

Adds face_count to assets (NULL = unprocessed, 0 = no faces, N = N faces).
Adds detection_model/detection_model_version to faces.
Adds centroid_vector/confirmation_count/representative_face_id to people (for future clustering).
Adds HNSW index on faces.embedding_vector and B-tree index on faces.asset_id.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f6g7h8i9j0k1"
down_revision: Union[str, Sequence[str], None] = "e5f6g7h8i9j0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_VIEW_DDL = "CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL"


def upgrade() -> None:
    # Drop and recreate active_assets view since it uses SELECT * and caches columns
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))

    # -- assets: face_count --
    op.add_column("assets", sa.Column("face_count", sa.Integer(), nullable=True))

    # -- faces: detection model tracking --
    op.add_column(
        "faces",
        sa.Column("detection_model", sa.String(), nullable=False, server_default="insightface"),
    )
    op.add_column(
        "faces",
        sa.Column("detection_model_version", sa.String(), nullable=False, server_default="buffalo_l"),
    )

    # -- people: clustering-ready columns (pgvector extension already enabled) --
    op.execute("ALTER TABLE people ADD COLUMN centroid_vector vector(512)")
    op.add_column(
        "people",
        sa.Column("confirmation_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "people",
        sa.Column(
            "representative_face_id",
            sa.String(),
            sa.ForeignKey("faces.face_id"),
            nullable=True,
        ),
    )

    # -- indexes --
    op.create_index("ix_faces_asset_id", "faces", ["asset_id"])
    op.execute(
        "CREATE INDEX ix_faces_embedding_hnsw ON faces "
        "USING hnsw (embedding_vector vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )

    # Recreate active_assets view with new columns
    op.execute(sa.text(_VIEW_DDL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS active_assets"))
    op.execute("DROP INDEX IF EXISTS ix_faces_embedding_hnsw")
    op.drop_index("ix_faces_asset_id", table_name="faces")
    op.drop_column("people", "representative_face_id")
    op.drop_column("people", "confirmation_count")
    op.drop_column("people", "centroid_vector")
    op.drop_column("faces", "detection_model_version")
    op.drop_column("faces", "detection_model")
    op.drop_column("assets", "face_count")
    op.execute(sa.text(_VIEW_DDL))
