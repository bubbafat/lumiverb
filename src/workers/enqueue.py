"""Enqueue worker jobs for a library."""

from sqlmodel import Session

from src.repository.tenant import AssetRepository, WorkerJobRepository


def enqueue_proxy_jobs(session: Session, library_id: str) -> int:
    """
    Create worker_jobs of type 'proxy' for all assets in library with
    status='pending' that don't already have a pending/claimed proxy job.
    Returns count of jobs enqueued.
    """
    asset_repo = AssetRepository(session)
    job_repo = WorkerJobRepository(session)
    pending_assets = asset_repo.list_pending_by_library(library_id)
    enqueued = 0
    for asset in pending_assets:
        if not job_repo.has_pending_job("proxy", asset.asset_id):
            job_repo.create("proxy", asset.asset_id)
            enqueued += 1
    return enqueued
