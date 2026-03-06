"""Base worker: claim jobs with FOR UPDATE SKIP LOCKED, lease, and run loop."""

import logging
import time
import uuid

from sqlmodel import Session

from src.core.config import get_settings
from src.models.tenant import WorkerJob
from src.repository.tenant import WorkerJobRepository

logger = logging.getLogger(__name__)


class BaseWorker:
    """Claim jobs from worker_jobs, process, complete or fail. Subclasses set job_type and implement process()."""

    job_type: str = ""  # subclasses set this

    def __init__(
        self,
        tenant_session: Session,
        concurrency: int = 1,
        once: bool = False,
        library_id: str | None = None,
    ) -> None:
        self._session = tenant_session
        self._concurrency = concurrency
        self._once = once
        self._library_id = library_id
        self._worker_id = f"worker_{uuid.uuid4().hex[:12]}"
        self._job_repo = WorkerJobRepository(tenant_session)
        self._settings = get_settings()

    def claim_job(self) -> WorkerJob | None:
        """Claim next pending job of this type using FOR UPDATE SKIP LOCKED."""
        return self._job_repo.claim_next(
            job_type=self.job_type,
            worker_id=self._worker_id,
            lease_minutes=self._settings.worker_lease_minutes,
            library_id=self._library_id,
        )

    def process(self, job: WorkerJob) -> None:
        """Subclasses implement this."""
        raise NotImplementedError

    def run(self) -> None:
        """Main loop: claim, process, complete or fail. Respects once flag."""
        while True:
            job = self.claim_job()
            if job is not None:
                try:
                    logger.info(
                        "claimed job_id=%s job_type=%s asset_id=%s",
                        job.job_id,
                        job.job_type,
                        job.asset_id,
                    )
                    self.process(job)
                    self._complete_job(job)
                    logger.info(
                        "completed job_id=%s job_type=%s asset_id=%s",
                        job.job_id,
                        job.job_type,
                        job.asset_id,
                    )
                except Exception as e:
                    self._fail_job(job, str(e))
                    logger.exception(
                        "failed job_id=%s job_type=%s asset_id=%s error=%s",
                        job.job_id,
                        job.job_type,
                        job.asset_id,
                        e,
                    )
            else:
                if self._once:
                    return
                time.sleep(self._settings.worker_idle_poll_seconds)

    def _complete_job(self, job: WorkerJob) -> None:
        self._job_repo.set_completed(job)

    def _fail_job(self, job: WorkerJob, error: str) -> None:
        self._job_repo.set_failed(job, error)
