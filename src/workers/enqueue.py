"""Enqueue worker jobs for a library."""

from datetime import datetime, timezone

from sqlalchemy import insert, text
from sqlmodel import Session
from ulid import ULID

from src.models.filter import AssetFilterSpec
from src.models.tenant import WorkerJob
from src.repository.tenant import AssetRepository, WorkerJobRepository

ENQUEUE_BATCH_SIZE = 1000


def _priority_for_job_type(job_type: str) -> int:
    """
    Return default priority for a job type.

    0 = urgent, 10 = normal, 20 = low.
    Proxy and video-preview jobs are urgent to minimize time-to-first-view.
    """
    if job_type in ("proxy", "video-preview"):
        return 0
    return 10


def _batch_insert_jobs(session: Session, jobs: list[dict]) -> int:
    """Bulk-INSERT jobs in batches of ENQUEUE_BATCH_SIZE. Returns count inserted."""
    total = 0
    for i in range(0, len(jobs), ENQUEUE_BATCH_SIZE):
        batch = jobs[i : i + ENQUEUE_BATCH_SIZE]
        session.execute(insert(WorkerJob), batch)
        total += len(batch)
    session.commit()
    return total


def enqueue_proxy_jobs(session: Session, library_id: str) -> int:
    """
    Enqueue proxy jobs for all pending assets in library that don't already
    have a pending/claimed proxy job.

    Uses a single SELECT to find eligible assets, then bulk INSERTs in
    batches of ENQUEUE_BATCH_SIZE to stay under Postgres parameter limits.
    Single commit at the end.

    Returns count of jobs enqueued.
    """
    stmt = text("""
        SELECT a.asset_id FROM assets a
        WHERE a.library_id = :library_id
          AND a.status = 'pending'
          AND NOT EXISTS (
            SELECT 1 FROM worker_jobs w
            WHERE w.asset_id = a.asset_id
              AND w.job_type = 'proxy'
              AND w.status IN ('pending', 'claimed')
          )
    """)
    rows = session.execute(stmt, {"library_id": library_id}).fetchall()
    if not rows:
        return 0

    now = datetime.now(timezone.utc)
    priority = _priority_for_job_type("proxy")
    jobs = [
        {
            "job_id": "job_" + str(ULID()),
            "job_type": "proxy",
            "asset_id": row[0],
            "status": "pending",
            "priority": priority,
            "created_at": now,
        }
        for row in rows
    ]
    return _batch_insert_jobs(session, jobs)


def enqueue_jobs_for_filter(
    session: Session,
    asset_filter: AssetFilterSpec,
    job_type: str,
    force: bool = False,
) -> int:
    """
    Enqueue jobs matching AssetFilterSpec. If force=True, cancels existing
    pending/claimed jobs first. Bulk INSERT in batches. Returns count enqueued.
    """
    asset_repo = AssetRepository(session)
    job_repo = WorkerJobRepository(session)

    asset_ids = asset_repo.query_for_enqueue(asset_filter, job_type, force)
    if not asset_ids:
        return 0

    if asset_filter.retry_failed:
        job_repo.cancel_failed_for_assets(asset_ids, job_type)
    elif force:
        job_repo.cancel_pending_for_assets(asset_ids, job_type)

    now = datetime.now(timezone.utc)
    priority = _priority_for_job_type(job_type)
    jobs = [
        {
            "job_id": "job_" + str(ULID()),
            "job_type": job_type,
            "asset_id": asset_id,
            "status": "pending",
            "priority": priority,
            "created_at": now,
        }
        for asset_id in asset_ids
    ]
    return _batch_insert_jobs(session, jobs)
