"""Uploads that survive a dropped connection (ADR-018 decision 9, phase 5).

Revision ID: b3e7c1f9d2a5
Revises: a8d4f2c6e1b9
Create Date: 2026-10-17

An upload of a clip's video preview or analysis proxy, sent in chunks
(src/server/uploads.py): what it is (the clip, the artifact, its size and
the whole file's SHA-256, how it was made), how many bytes have arrived,
when it expires (a day after its last chunk), and once finished, its
answer, so a finish sent again gets it. Its bytes wait in the server's own
folder until it's finished. One open upload per clip, artifact, size and
hash: a client starting the same one again (after a restart) picks it up
where it was.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3e7c1f9d2a5"
down_revision: str | Sequence[str] | None = "a8d4f2c6e1b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "uploads",
        sa.Column("upload_id", sa.Text(), primary_key=True),
        sa.Column("asset_id", sa.Text(), sa.ForeignKey("assets.asset_id", ondelete="CASCADE"), nullable=False),
        sa.Column("artifact", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("lineage", postgresql.JSONB(), nullable=False),
        sa.Column("received", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint("artifact IN ('video_preview', 'analysis_proxy')", name="uploads_artifact"),
        sa.CheckConstraint("size > 0 AND received >= 0 AND received <= size", name="uploads_bytes"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="uploads_sha256"),
    )
    op.create_index("uploads_open", "uploads", ["asset_id", "artifact", "size", "sha256"], unique=True,
                    postgresql_where=sa.text("finished_at IS NULL"))
    op.create_index("uploads_expires_at", "uploads", ["expires_at"])


def downgrade() -> None:
    op.drop_index("uploads_expires_at", table_name="uploads")
    op.drop_index("uploads_open", table_name="uploads")
    op.drop_table("uploads")
