"""Drop worker_jobs, pipeline_locks, scans tables and last_scan_id column.

Queue-based pipeline replaced by client-side ingest. Scans lifecycle
replaced by atomic POST /v1/ingest endpoint.

Drops:
- worker_jobs table (and its indexes)
- pipeline_locks table
- scans table
- assets.last_scan_id column (FK to scans)
"""

revision = "j9k0l1m2n3o4"
down_revision = "i8j9k0l1m2n3"

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    # Drop and recreate the active_assets view since it uses SELECT *
    # and dropping columns invalidates its cached column list.
    op.execute("DROP VIEW IF EXISTS active_assets")
    # Drop last_scan_id column (CASCADE drops the FK constraint automatically)
    op.execute("ALTER TABLE assets DROP COLUMN IF EXISTS last_scan_id CASCADE")
    op.execute("DROP TABLE IF EXISTS worker_jobs CASCADE")
    op.execute("DROP TABLE IF EXISTS pipeline_locks CASCADE")
    op.execute("DROP TABLE IF EXISTS scans CASCADE")
    # Recreate the view with the updated column set
    op.execute("CREATE VIEW active_assets AS SELECT * FROM assets WHERE deleted_at IS NULL")


def downgrade() -> None:
    # Recreate scans table
    op.create_table(
        "scans",
        sa.Column("scan_id", sa.String(), primary_key=True),
        sa.Column("library_id", sa.String(), sa.ForeignKey("libraries.library_id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("root_path_override", sa.String(), nullable=True),
        sa.Column("worker_id", sa.String(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("files_discovered", sa.Integer(), nullable=True),
        sa.Column("files_added", sa.Integer(), nullable=True),
        sa.Column("files_updated", sa.Integer(), nullable=True),
        sa.Column("files_skipped", sa.Integer(), nullable=True),
        sa.Column("files_missing", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Recreate pipeline_locks table
    op.create_table(
        "pipeline_locks",
        sa.Column("lock_id", sa.String(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False, unique=True),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Recreate worker_jobs table
    op.create_table(
        "worker_jobs",
        sa.Column("job_id", sa.String(), primary_key=True),
        sa.Column("job_type", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.asset_id"), nullable=True),
        sa.Column("scene_id", sa.String(), sa.ForeignKey("video_scenes.scene_id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("worker_id", sa.String(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Restore last_scan_id column
    op.add_column("assets", sa.Column("last_scan_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "assets_last_scan_id_fkey", "assets", "scans", ["last_scan_id"], ["scan_id"]
    )
