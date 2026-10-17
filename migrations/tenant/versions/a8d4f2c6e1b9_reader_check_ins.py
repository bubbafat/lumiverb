"""A library's reader checks in, and a mark per library says when to fetch what changed (ADR-018 phase 4).

Revision ID: a8d4f2c6e1b9
Revises: f4c2a9e7b1d3
Create Date: 2026-10-17

- libraries.reader_mark: moves whenever something changes on the server
  that its reader acts on (src/server/check_ins.py): the library's root,
  status, reader or scans pause, a scan asked for; its path filters, or the
  account's; a pause of any switch; any producer's settings or new version
  seen; failing clips asked to be tried again; a clip's preview, video
  preview or analysis proxy lost (the missing-file repair cleared its key).
  Triggers move it, so no writer can forget, in the same transaction as the
  change: a reader that sees the new mark sees the change.
- libraries.scan_requested_at: a scan of the whole library asked for (Scan
  now), until a whole-library scan started after it finishes.
- reader_check_ins: each library's latest check-in from its reader, one row
  a library.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8d4f2c6e1b9"
down_revision: str | Sequence[str] | None = "f4c2a9e7b1d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("libraries", sa.Column("reader_mark", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("libraries", sa.Column("scan_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "reader_check_ins",
        sa.Column("library_id", sa.Text(), sa.ForeignKey("libraries.library_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("reader", sa.Text(), nullable=False),
        sa.Column("client", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("waiting_files", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("waiting_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_full_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mark", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('scanning', 'idle', 'paused', 'away')", name="reader_check_ins_state"),
    )
    op.execute("""
        CREATE FUNCTION move_reader_marks(lib text) RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            -- In one order, so two transactions moving every mark wait rather than deadlock.
            UPDATE libraries SET reader_mark = reader_mark + 1
             WHERE library_id IN (SELECT library_id FROM libraries WHERE lib IS NULL OR library_id = lib
                                  ORDER BY library_id FOR UPDATE);
        END $$;

        CREATE FUNCTION reader_marks_all() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM move_reader_marks(NULL);
            RETURN NULL;
        END $$;

        CREATE FUNCTION reader_mark_of_filters() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM move_reader_marks(CASE WHEN TG_OP = 'DELETE' THEN OLD.library_id ELSE NEW.library_id END);
            RETURN NULL;
        END $$;

        CREATE FUNCTION reader_mark_of_clip() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM move_reader_marks(NEW.library_id);
            RETURN NULL;
        END $$;

        CREATE FUNCTION reader_mark_of_library() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.reader_mark := OLD.reader_mark + 1;
            RETURN NEW;
        END $$;

        CREATE TRIGGER reader_mark_library BEFORE UPDATE ON libraries FOR EACH ROW
            WHEN (OLD.root_path IS DISTINCT FROM NEW.root_path OR OLD.status IS DISTINCT FROM NEW.status
                  OR OLD.reader IS DISTINCT FROM NEW.reader
                  OR OLD.scans_paused_at IS DISTINCT FROM NEW.scans_paused_at
                  OR (NEW.scan_requested_at IS NOT NULL
                      AND OLD.scan_requested_at IS DISTINCT FROM NEW.scan_requested_at))
            EXECUTE FUNCTION reader_mark_of_library();
        CREATE TRIGGER reader_mark_filters AFTER INSERT OR UPDATE OR DELETE ON library_path_filters
            FOR EACH ROW EXECUTE FUNCTION reader_mark_of_filters();
        CREATE TRIGGER reader_mark_filter_defaults AFTER INSERT OR UPDATE OR DELETE ON tenant_path_filter_defaults
            FOR EACH STATEMENT EXECUTE FUNCTION reader_marks_all();
        CREATE TRIGGER reader_mark_pauses AFTER INSERT OR UPDATE OR DELETE ON producer_pauses
            FOR EACH STATEMENT EXECUTE FUNCTION reader_marks_all();
        CREATE TRIGGER reader_mark_settings AFTER INSERT OR UPDATE ON system_metadata FOR EACH ROW
            WHEN (NEW.key LIKE 'producer.%' OR NEW.key = 'scheduler.retry_at')
            EXECUTE FUNCTION reader_marks_all();
        CREATE TRIGGER reader_mark_settings_gone AFTER DELETE ON system_metadata FOR EACH ROW
            WHEN (OLD.key LIKE 'producer.%' OR OLD.key = 'scheduler.retry_at')
            EXECUTE FUNCTION reader_marks_all();
        CREATE TRIGGER reader_mark_lost AFTER UPDATE OF proxy_key, video_preview_key, analysis_proxy_key ON assets
            FOR EACH ROW
            WHEN ((OLD.proxy_key IS NOT NULL AND NEW.proxy_key IS NULL)
                  OR (OLD.video_preview_key IS NOT NULL AND NEW.video_preview_key IS NULL)
                  OR (OLD.analysis_proxy_key IS NOT NULL AND NEW.analysis_proxy_key IS NULL))
            EXECUTE FUNCTION reader_mark_of_clip();
    """)


def downgrade() -> None:
    op.execute("""
        DROP TRIGGER IF EXISTS reader_mark_lost ON assets;
        DROP TRIGGER IF EXISTS reader_mark_settings_gone ON system_metadata;
        DROP TRIGGER IF EXISTS reader_mark_settings ON system_metadata;
        DROP TRIGGER IF EXISTS reader_mark_pauses ON producer_pauses;
        DROP TRIGGER IF EXISTS reader_mark_filter_defaults ON tenant_path_filter_defaults;
        DROP TRIGGER IF EXISTS reader_mark_filters ON library_path_filters;
        DROP TRIGGER IF EXISTS reader_mark_library ON libraries;
        DROP FUNCTION IF EXISTS reader_mark_of_library();
        DROP FUNCTION IF EXISTS reader_mark_of_clip();
        DROP FUNCTION IF EXISTS reader_mark_of_filters();
        DROP FUNCTION IF EXISTS reader_marks_all();
        DROP FUNCTION IF EXISTS move_reader_marks(text);
    """)
    op.drop_table("reader_check_ins")
    op.drop_column("libraries", "scan_requested_at")
    op.drop_column("libraries", "reader_mark")
