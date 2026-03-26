"""Base worker: API-only. Claims jobs via GET /v1/jobs/next, complete/fail via POST."""

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext


class BlockJob(Exception):
    """Raise from process() to immediately block the job (permanent failure, never retried).
    Use for invariant violations like wrong media type — retrying would always fail."""

from rich.console import Console

from src.cli.progress import UnifiedProgress, UnifiedProgressSpec
from src.workers.output_events import emit_event

WORKER_IDLE_POLL_SECONDS = 5.0

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
        path_prefix: str | None = None,
        suppress_base_progress: bool = False,
        output_mode: str = "human",
    ) -> None:
        self._client = client
        self._once = once
        self._library_id = library_id
        self._path_prefix = path_prefix
        self._console = Console()
        self._suppress_base_progress = suppress_base_progress
        self._output_mode = output_mode

    def _emit_event(self, event: str, **fields: object) -> None:
        """Emit a structured event in jsonl mode. event: start|batch|complete|error|warning."""
        payload = {"event": event, "stage": self.job_type, **fields}
        emit_event(self._output_mode, payload)

    def _pending_count(self) -> int:
        """GET /v1/jobs/pending — count of pending/claimed jobs for progress total."""
        params: dict[str, str] = {"job_type": self.job_type}
        if self._library_id:
            params["library_id"] = self._library_id
        if self._path_prefix:
            params["path_prefix"] = self._path_prefix
        resp = self._client.get("/v1/jobs/pending", params=params)
        resp.raise_for_status()
        data = resp.json()
        return int(data.get("pending", 0))

    def claim_job(self) -> dict | None:
        """
        GET /v1/jobs/next?job_type=...&library_id=...
        Returns job dict or None if no jobs available (204).
        """
        params: dict[str, str] = {"job_type": self.job_type}
        if self._library_id:
            params["library_id"] = self._library_id
        if self._path_prefix:
            params["path_prefix"] = self._path_prefix
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

    def block_job(self, job_id: str, error_message: str) -> None:
        """POST /v1/jobs/{job_id}/block — permanently block without incrementing fail_count."""
        self._client.post(f"/v1/jobs/{job_id}/block", json={"error_message": error_message})

    def process(self, job: dict) -> dict | None:
        """
        Subclasses implement this. Receives job dict from claim_job.
        Returns result dict to pass to complete_job, or None for no result payload.
        Raise any exception to trigger fail_job.
        """
        raise NotImplementedError

    def _wait_for_completion(self, future: Future | None) -> bool:
        """Wait for a pipelined complete/fail/block call. Returns True if it succeeded."""
        if future is None:
            return True
        try:
            future.result()
            return True
        except Exception:
            logger.exception("Background completion call failed (job will be re-claimed)")
            return False

    def run(self) -> None:
        """Main loop: claim, process, complete or fail.

        Pipelined: the completion API call for job N runs in a background thread
        while job N+1 is being processed. This overlaps network I/O (uploading
        artifacts via complete_job) with local compute (processing the next image).
        The system is idempotent, so if a background completion fails, the job
        will simply be re-claimed on the next cycle.
        """
        processed = 0
        failed = 0
        last_rel_path = ""
        pending = self._pending_count()
        use_jsonl = self._output_mode == "jsonl"
        self._emit_event(
            "start",
            library_id=self._library_id or "",
            path_prefix=self._path_prefix or "",
        )
        spec = UnifiedProgressSpec(
            label=f"Processing {self.job_type} jobs",
            unit="jobs",
            counters=["done", "failed"],
            total=pending if pending > 0 else None,
        )
        progress_ctx = (
            nullcontext()
            if (self._suppress_base_progress or use_jsonl)
            else UnifiedProgress(self._console, spec)
        )

        # Single-thread pool for pipelining the completion call.
        completion_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="complete")
        inflight: Future | None = None

        with progress_ctx as bar:
            while True:
                job = self.claim_job()
                if job is None:
                    # No more work — wait for any in-flight completion before exiting.
                    self._wait_for_completion(inflight)
                    inflight = None
                    if self._once:
                        if bar is not None:
                            bar.finish()
                        break
                    time.sleep(WORKER_IDLE_POLL_SECONDS)
                    continue
                job_id = job["job_id"]
                try:
                    logger.info(
                        "claimed job_id=%s job_type=%s asset_id=%s",
                        job_id,
                        job.get("job_type"),
                        job.get("asset_id"),
                    )
                    last_rel_path = job.get("rel_path", "") or last_rel_path
                    result = self.process(job)

                    # Wait for the previous job's completion before submitting the next one.
                    # This ensures at most one completion is in flight at a time.
                    self._wait_for_completion(inflight)

                    # Submit this job's completion to the background thread.
                    inflight = completion_pool.submit(self.complete_job, job_id, result or {})

                    processed += 1
                    if bar is not None:
                        bar.update(
                            completed=processed + failed,
                            done=processed,
                            failed=failed,
                        )
                    logger.info("completed job_id=%s", job_id)
                    if not use_jsonl:
                        print(f"{self.job_type} ✓ {job.get('rel_path', job_id)}", flush=True)
                    self._emit_event(
                        "batch",
                        processed=processed,
                        failed=failed,
                        library_id=self._library_id or "",
                        rel_path=last_rel_path,
                    )
                except BlockJob as e:
                    self._wait_for_completion(inflight)
                    inflight = None
                    logger.warning("blocking job_id=%s reason=%s", job_id, e)
                    self.block_job(job_id, str(e))
                    failed += 1
                    if bar is not None:
                        bar.update(
                            completed=processed + failed,
                            done=processed,
                            failed=failed,
                        )
                    if not use_jsonl:
                        print(f"{self.job_type} ⊘ {job.get('rel_path', job_id)}: {e}", flush=True)
                    self._emit_event(
                        "error",
                        message=str(e),
                        rel_path=job.get("rel_path", ""),
                        processed=processed,
                        failed=failed,
                    )
                except Exception as e:
                    self._wait_for_completion(inflight)
                    inflight = None
                    logger.exception("failed job_id=%s error=%s", job_id, e)
                    self.fail_job(job_id, str(e))
                    failed += 1
                    if bar is not None:
                        bar.update(
                            completed=processed + failed,
                            done=processed,
                            failed=failed,
                        )
                    if not use_jsonl:
                        print(f"{self.job_type} ✗ {job.get('rel_path', job_id)}: {e}", flush=True)
                    self._emit_event(
                        "error",
                        message=str(e),
                        rel_path=job.get("rel_path", ""),
                        processed=processed,
                        failed=failed,
                    )

        # Wait for final in-flight completion and shut down the pool.
        self._wait_for_completion(inflight)
        completion_pool.shutdown(wait=True)

        self._emit_event("complete", processed=processed, failed=failed)
        if not use_jsonl and not self._suppress_base_progress:
            self._console.print(
                f"Done: {processed:,} succeeded, {failed:,} failed"
            )
