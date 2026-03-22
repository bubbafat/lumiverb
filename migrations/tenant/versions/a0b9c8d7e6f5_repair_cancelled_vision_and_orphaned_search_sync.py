"""Repair cancelled ai_vision jobs and orphaned search_sync entries.

Revision ID: a0b9c8d7e6f5
Revises: z3a4b5c6d7e8
Create Date: 2026-03-22

Two classes of bad data addressed:

1. Cancelled ai_vision jobs — images with proxy_key set but no asset_metadata
   and no active job, whose only worker_jobs entry is 'cancelled'.  The TOCTOU
   dedup migration (b6c7d8e9f0a1) cancelled older duplicate jobs, leaving some
   assets with a surviving active job (fine) but potentially others with only
   cancelled entries if both duplicates were later cancelled by other means.
   More broadly, previous operational fixes cancelled ai_vision jobs without
   checking whether the asset had already been processed.  z3a4b5c6d7e8
   addressed 'failed' jobs; this migration addresses 'cancelled' jobs the same
   way: reset them to 'pending' so the vision worker picks them up.

2. Orphaned search_sync entries — assets with a proxy that have never appeared
   in search_sync_queue with status 'pending', 'processing', or 'synced'.
   These assets are invisible to the search sync worker and will never reach
   Quickwit without intervention.  Insert a pending 'upsert' entry for each.
   The partial unique index (uq_ssq_pending_asset_scene, added in e3f4a5b6c7d8)
   makes the insert idempotent via ON CONFLICT DO NOTHING.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

try:
    from ulid import ULID
    _HAS_ULID = True
except ImportError:
    _HAS_ULID = False

log = logging.getLogger("alembic.runtime.migration")

revision: str = "a0b9c8d7e6f5"
down_revision: Union[str, Sequence[str], None] = ("z3a4b5c6d7e8", "f4a5b6c7d8e9")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BATCH = 500


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # 1. Reset cancelled ai_vision jobs to pending.
    #    Mirrors z3a4b5c6d7e8 which did the same for 'failed' jobs.
    #    Conditions (identical to the normal enqueue eligibility check):
    #      - image asset with proxy_key set
    #      - no asset_metadata row for the library's current vision model
    #      - no active (pending/claimed) ai_vision job already running
    #      - library not trashed, asset not soft-deleted
    # ------------------------------------------------------------------
    result = bind.execute(
        text(
            """
            UPDATE worker_jobs wj
            SET status           = 'pending',
                worker_id        = NULL,
                claimed_at       = NULL,
                lease_expires_at = NULL,
                completed_at     = NULL,
                error_message    = NULL
            FROM assets a
            JOIN libraries l ON l.library_id = a.library_id
            WHERE a.asset_id   = wj.asset_id
              AND wj.job_type  = 'ai_vision'
              AND wj.status    = 'cancelled'
              AND a.proxy_key  IS NOT NULL
              AND a.media_type LIKE 'image/%'
              AND a.deleted_at IS NULL
              AND l.status     != 'trashed'
              AND NOT EXISTS (
                  SELECT 1 FROM asset_metadata m
                  WHERE m.asset_id = a.asset_id
                    AND m.model_id = l.vision_model_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM worker_jobs w2
                  WHERE w2.asset_id = wj.asset_id
                    AND w2.job_type = 'ai_vision'
                    AND w2.status   IN ('pending', 'claimed')
              )
            """
        )
    )
    log.info("repair: reset %d cancelled ai_vision job(s) to pending", result.rowcount)

    # ------------------------------------------------------------------
    # 2. Re-enqueue search_sync for orphaned assets.
    #    Target: assets with proxy_key that have no pending, processing,
    #    or successfully synced entry in search_sync_queue.  These are
    #    invisible to the search sync worker.
    #    Requires ulid package (present in normal runtime env); skips
    #    gracefully if unavailable (e.g. bare alembic-only env).
    # ------------------------------------------------------------------
    if not _HAS_ULID:
        log.warning(
            "repair: ulid package not available — skipping search_sync re-enqueue"
        )
        return

    rows = bind.execute(
        text(
            """
            SELECT a.asset_id
            FROM assets a
            WHERE a.proxy_key  IS NOT NULL
              AND a.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM search_sync_queue ssq
                  WHERE ssq.asset_id = a.asset_id
                    AND ssq.status   IN ('pending', 'processing', 'synced')
              )
            ORDER BY a.asset_id
            """
        )
    ).fetchall()

    asset_ids = [r[0] for r in rows]
    if not asset_ids:
        log.info("repair: no orphaned search_sync assets found")
        return

    now = datetime.now(timezone.utc)
    inserts = [
        {
            "sync_id": "ssq_" + str(ULID()),
            "asset_id": asset_id,
            "operation": "upsert",
            "status": "pending",
            "created_at": now,
        }
        for asset_id in asset_ids
    ]

    inserted = 0
    for i in range(0, len(inserts), _BATCH):
        batch = inserts[i : i + _BATCH]
        r = bind.execute(
            text(
                """
                INSERT INTO search_sync_queue
                    (sync_id, asset_id, scene_id, operation, status, created_at)
                VALUES
                    (:sync_id, :asset_id, NULL, :operation, :status, :created_at)
                ON CONFLICT DO NOTHING
                """
            ),
            batch,
        )
        inserted += r.rowcount

    log.info(
        "repair: enqueued search_sync for %d/%d orphaned asset(s)",
        inserted,
        len(asset_ids),
    )


def downgrade() -> None:
    # Data-only migration: cannot reliably reverse.
    # The original 'cancelled' job state is gone; re-cancelled jobs that have
    # since completed would be incorrectly re-cancelled.  The inserted
    # search_sync rows are at best low-cost noise to remove manually if needed.
    pass
