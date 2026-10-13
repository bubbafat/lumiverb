"""A scan's session on the server: what it found, and what the server decided (ADR-018 decision 12).

Revision ID: e7b9d1f3a5c2
Revises: d4f7a2c9e1b3
Create Date: 2026-10-13

- scan_sessions: one scan of a library, or a folder of it, while it runs: its
  options, when it started (a clip made or changed since is never taken for
  gone by it), the folders it couldn't list, when it was planned and
  finished, and when it expires (a day after its last call).
- scan_found: each file the scan found on disk, keyed by its path, and what
  the server planned for it and decided once it had its content. Deleted with
  its scan, and when the scan finishes.

Not named `scans`: that was the old scan lifecycle's table, which
j9k0l1m2n3o4 dropped and its downgrade makes again.

Down: drops both (a scan under way has to start again).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision: str = "e7b9d1f3a5c2"
down_revision: str | Sequence[str] | None = "d4f7a2c9e1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scan_sessions",
        sa.Column("scan_id", sa.String(), primary_key=True),
        sa.Column("library_id", sa.String(), sa.ForeignKey("libraries.library_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("scope", sa.String(), nullable=False, server_default=""),
        sa.Column("media_type", sa.String(), nullable=False, server_default="all"),
        sa.Column("thorough", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("force", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("moves", sa.String(), nullable=False, server_default="ask"),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unlisted", ARRAY(sa.Text()), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("media_type IN ('all', 'image', 'video')", name="ck_scan_sessions_media_type"),
        sa.CheckConstraint("moves IN ('apply', 'skip', 'ask')", name="ck_scan_sessions_moves"),
    )
    op.create_index("ix_scan_sessions_library_id", "scan_sessions", ["library_id"])
    op.create_index("ix_scan_sessions_expires_at", "scan_sessions", ["expires_at"])
    op.create_table(
        "scan_found",
        sa.Column("scan_id", sa.String(), sa.ForeignKey("scan_sessions.scan_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("rel_path", sa.String(), primary_key=True),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("file_mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("media_type", sa.String(), nullable=False),
        sa.Column("settling", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("action", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.Column("sha256", sa.String(), nullable=True),
        sa.Column("decision", sa.String(), nullable=True),
        sa.Column("moved_from", sa.String(), nullable=True),
    )
    # The clips a scan's moves have claimed, so two files never take one.
    op.create_index("ix_scan_found_moving", "scan_found", ["scan_id", "asset_id"],
                    postgresql_where=sa.text("decision IN ('moved', 'move_candidate', 'move_skipped')"))


def downgrade() -> None:
    op.drop_index("ix_scan_found_moving", table_name="scan_found")
    op.drop_table("scan_found")
    op.drop_index("ix_scan_sessions_expires_at", table_name="scan_sessions")
    op.drop_index("ix_scan_sessions_library_id", table_name="scan_sessions")
    op.drop_table("scan_sessions")
