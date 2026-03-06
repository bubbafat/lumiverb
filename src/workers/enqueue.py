"""Enqueue worker jobs for a library."""

from datetime import datetime, timezone

from sqlalchemy import insert, text
from sqlmodel import Session
from ulid import ULID

from src.models.tenant import WorkerJob

ENQUEUE_BATCH_SIZE = 1000


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
    jobs = [
        {
            "job_id": "job_" + str(ULID()),
            "job_type": "proxy",
            "asset_id": row[0],
            "status": "pending",
            "created_at": now,
        }
        for row in rows
    ]

    total = 0
    for i in range(0, len(jobs), ENQUEUE_BATCH_SIZE):
        batch = jobs[i : i + ENQUEUE_BATCH_SIZE]
        session.execute(insert(WorkerJob), batch)
        total += len(batch)
    session.commit()
    return total
