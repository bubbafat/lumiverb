"""Base worker: API-only. Claims jobs via GET /v1/jobs/next, complete/fail via POST."""

import logging
import time

from rich.console import Console

from src.cli.progress import UnifiedProgress, UnifiedProgressSpec
from src.core.config import get_settings

logger = logging.getLogger(__name__)


class BaseWorker:
    """
    API-only worker base. Never touches the database directly.
    Uses LumiverbClient for all communication with the system.
    Subclasses implement process(job: dict) -> dict | None.
    """

    job_type: str = ""

    def __init__(
        self,
        client: object,
        concurrency: int = 1,
        once: bool = False,
        library_id: str | None = None,
    ) -> None:
        self._client = client
        self._concurrency = concurrency
        self._once = once
        self._library_id = library_id
        self._console = Console()

    def claim_job(self) -> dict | None:
        """
        GET /v1/jobs/next?job_type=...&library_id=...
        Returns job dict or None if no jobs available (204).
        """
        params: dict[str, str] = {"job_type": self.job_type}
        if self._library_id:
            params["library_id"] = self._library_id
        resp = self._client.get("/v1/jobs/next", params=params)
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json()

    def complete_job(self, job_id: str, result: dict) -> None:
        """POST /v1/jobs/{job_id}/complete with result payload."""
        self._client.post(f"/v1/jobs/{job_id}/complete", json=result)

    def fail_job(self, job_id: str, error_message: str) -> None:
        """POST /v1/jobs/{job_id}/fail"""
        self._client.post(f"/v1/jobs/{job_id}/fail", json={"error_message": error_message})

    def process(self, job: dict) -> dict | None:
        """
        Subclasses implement this. Receives job dict from claim_job.
        Returns result dict to pass to complete_job, or None for no result payload.
        Raise any exception to trigger fail_job.
        """
        raise NotImplementedError

    def run(self) -> None:
        """Main loop: claim, process, complete or fail. Respects once flag."""
        settings = get_settings()
        processed = 0
        failed = 0
        spec = UnifiedProgressSpec(
            label=f"Processing {self.job_type} jobs",
            unit="jobs",
            counters=["done", "failed"],
            total=None,
        )
        with UnifiedProgress(self._console, spec) as bar:
            while True:
                job = self.claim_job()
                if job is None:
                    if self._once:
                        bar.finish()
                        break
                    time.sleep(settings.worker_idle_poll_seconds)
                    continue
                job_id = job["job_id"]
                try:
                    logger.info(
                        "claimed job_id=%s job_type=%s asset_id=%s",
                        job_id,
                        job.get("job_type"),
                        job.get("asset_id"),
                    )
                    result = self.process(job)
                    self.complete_job(job_id, result or {})
                    processed += 1
                    bar.update(
                        completed=processed + failed,
                        done=processed,
                        failed=failed,
                    )
                    logger.info("completed job_id=%s", job_id)
                except Exception as e:
                    logger.exception("failed job_id=%s error=%s", job_id, e)
                    self.fail_job(job_id, str(e))
                    failed += 1
                    bar.update(
                        completed=processed + failed,
                        done=processed,
                        failed=failed,
                    )

        self._console.print(
            f"Done: {processed:,} succeeded, {failed:,} failed"
        )
