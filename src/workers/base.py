"""Base worker: API-only. Claims jobs via GET /v1/jobs/next, complete/fail via POST."""

import logging
import threading
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
        self._concurrency = max(1, concurrency)
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

    def _worker_loop(
        self,
        *,
        stats: "_WorkerStats",
        completion_pool: ThreadPoolExecutor,
        bar: object | None,
    ) -> None:
        """Single worker loop: claim → process → complete. Runs in its own thread
        when concurrency > 1, or on the main thread when concurrency == 1."""
        use_jsonl = self._output_mode == "jsonl"
        inflight: Future | None = None

        while not stats.stopping:
            job = self.claim_job()
            if job is None:
                self._wait_for_completion(inflight)
                inflight = None
                if self._once:
                    return
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
                result = self.process(job)

                # Wait for this thread's previous completion before submitting the next.
                self._wait_for_completion(inflight)
                inflight = completion_pool.submit(self.complete_job, job_id, result or {})

                with stats.lock:
                    stats.processed += 1
                    stats.last_rel_path = job.get("rel_path", "") or stats.last_rel_path
                    p, f = stats.processed, stats.failed
                    rel = stats.last_rel_path
                if bar is not None:
                    bar.update(completed=p + f, done=p, failed=f)
                logger.info("completed job_id=%s", job_id)
                if not use_jsonl:
                    print(f"{self.job_type} ✓ {job.get('rel_path', job_id)}", flush=True)
                self._emit_event(
                    "batch",
                    processed=p,
                    failed=f,
                    library_id=self._library_id or "",
                    rel_path=rel,
                )
            except BlockJob as e:
                self._wait_for_completion(inflight)
                inflight = None
                logger.warning("blocking job_id=%s reason=%s", job_id, e)
                self.block_job(job_id, str(e))
                with stats.lock:
                    stats.failed += 1
                    p, f = stats.processed, stats.failed
                if bar is not None:
                    bar.update(completed=p + f, done=p, failed=f)
                if not use_jsonl:
                    print(f"{self.job_type} ⊘ {job.get('rel_path', job_id)}: {e}", flush=True)
                self._emit_event(
                    "error",
                    message=str(e),
                    rel_path=job.get("rel_path", ""),
                    processed=p,
                    failed=f,
                )
            except Exception as e:
                self._wait_for_completion(inflight)
                inflight = None
                logger.exception("failed job_id=%s error=%s", job_id, e)
                self.fail_job(job_id, str(e))
                with stats.lock:
                    stats.failed += 1
                    p, f = stats.processed, stats.failed
                if bar is not None:
                    bar.update(completed=p + f, done=p, failed=f)
                if not use_jsonl:
                    print(f"{self.job_type} ✗ {job.get('rel_path', job_id)}: {e}", flush=True)
                self._emit_event(
                    "error",
                    message=str(e),
                    rel_path=job.get("rel_path", ""),
                    processed=p,
                    failed=f,
                )

        # Drain this thread's in-flight completion on shutdown.
        self._wait_for_completion(inflight)

    def run(self) -> None:
        """Main entry point. Spawns concurrent worker threads if concurrency > 1,
        otherwise runs a single loop on the main thread.

        Each thread pipelines its own completion calls: the upload for job N
        runs in the background while job N+1 is being processed.
        """
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

        stats = _WorkerStats()
        # One completion thread per worker thread for pipelining.
        completion_pool = ThreadPoolExecutor(
            max_workers=self._concurrency,
            thread_name_prefix="complete",
        )

        with progress_ctx as bar:
            if self._concurrency == 1:
                self._worker_loop(stats=stats, completion_pool=completion_pool, bar=bar)
            else:
                threads: list[threading.Thread] = []
                for i in range(self._concurrency):
                    t = threading.Thread(
                        target=self._worker_loop,
                        kwargs={"stats": stats, "completion_pool": completion_pool, "bar": bar},
                        name=f"worker-{i}",
                        daemon=True,
                    )
                    t.start()
                    threads.append(t)
                for t in threads:
                    t.join()
            if bar is not None:
                bar.finish()

        completion_pool.shutdown(wait=True)

        self._emit_event("complete", processed=stats.processed, failed=stats.failed)
        if not use_jsonl and not self._suppress_base_progress:
            self._console.print(
                f"Done: {stats.processed:,} succeeded, {stats.failed:,} failed"
            )


class _WorkerStats:
    """Thread-safe counters shared across concurrent worker loops."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.processed = 0
        self.failed = 0
        self.last_rel_path = ""
        self.stopping = False
