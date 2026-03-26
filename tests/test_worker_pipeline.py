"""Tests for pipelined completion in BaseWorker.run().

The worker overlaps complete_job (network I/O) with process() for the next job.
These tests verify:
  - Completion is called in a background thread (pipelining happens)
  - Processing of job N+1 starts before completion of job N finishes
  - Failures in background completion are handled gracefully
  - Block/fail paths still wait for in-flight completions
  - Shutdown waits for the final in-flight completion
"""

import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest

from src.workers.base import BaseWorker, BlockJob


class StubWorker(BaseWorker):
    """Minimal worker for testing. Tracks call order via a shared list."""

    job_type = "test"

    def __init__(self, client: object, process_fn=None, **kwargs):
        super().__init__(client=client, suppress_base_progress=True, **kwargs)
        self._process_fn = process_fn or (lambda job: {"ok": True})

    def process(self, job: dict) -> dict | None:
        return self._process_fn(job)


def _mock_client_with_jobs(jobs: list[dict]) -> MagicMock:
    """Create a mock client that returns jobs in order, then 204."""
    client = MagicMock()
    claim_responses = []
    for job in jobs:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = job
        resp.raise_for_status.return_value = None
        claim_responses.append(resp)

    # Final response: no more jobs (204)
    empty_resp = MagicMock()
    empty_resp.status_code = 204
    claim_responses.append(empty_resp)

    client.get.side_effect = lambda path, **kw: (
        claim_responses.pop(0) if path == "/v1/jobs/next" and claim_responses
        else _pending_resp() if path == "/v1/jobs/pending"
        else empty_resp
    )

    client.post.return_value = MagicMock()
    return client


def _pending_resp() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"pending": 0}
    resp.raise_for_status.return_value = None
    return resp


@pytest.mark.fast
def test_pipelined_completion_overlaps_with_processing() -> None:
    """Verify that complete_job for job N runs concurrently with process() for job N+1."""
    events: list[str] = []
    complete_started = threading.Event()
    complete_proceed = threading.Event()

    jobs = [
        {"job_id": "j1", "rel_path": "a.jpg"},
        {"job_id": "j2", "rel_path": "b.jpg"},
    ]
    client = _mock_client_with_jobs(jobs)

    # Make complete_job block until we signal it, so we can prove overlap.
    original_post = client.post

    def slow_post(path: str, **kwargs) -> MagicMock:
        if "/complete" in path:
            complete_started.set()
            complete_proceed.wait(timeout=5)
        return MagicMock()

    client.post.side_effect = slow_post

    def process_fn(job: dict) -> dict:
        if job["job_id"] == "j2":
            # When processing j2, j1's completion should already be in flight.
            assert complete_started.wait(timeout=5), "complete_job for j1 should have started"
            complete_proceed.set()  # Let it finish
        events.append(f"process:{job['job_id']}")
        return {"ok": True}

    worker = StubWorker(client=client, process_fn=process_fn, once=True)
    worker.run()

    assert events == ["process:j1", "process:j2"]


@pytest.mark.fast
def test_completion_failure_does_not_crash_worker() -> None:
    """If a background complete_job fails, the worker continues processing."""
    jobs = [
        {"job_id": "j1", "rel_path": "a.jpg"},
        {"job_id": "j2", "rel_path": "b.jpg"},
    ]
    client = _mock_client_with_jobs(jobs)

    call_count = 0

    def failing_post(path: str, **kwargs) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if "/complete" in path and "j1" in path:
            raise ConnectionError("upload failed")
        return MagicMock()

    client.post.side_effect = failing_post

    processed = []

    def process_fn(job: dict) -> dict:
        processed.append(job["job_id"])
        return {"ok": True}

    worker = StubWorker(client=client, process_fn=process_fn, once=True)
    worker.run()

    # Both jobs should have been processed despite j1's completion failing.
    assert processed == ["j1", "j2"]


@pytest.mark.fast
def test_block_job_waits_for_inflight() -> None:
    """When a job is blocked, the worker waits for any in-flight completion first."""
    events: list[str] = []

    jobs = [
        {"job_id": "j1", "rel_path": "a.jpg"},
        {"job_id": "j2", "rel_path": "b.jpg", "media_type": "video"},
    ]
    client = _mock_client_with_jobs(jobs)

    original_post = client.post

    def tracking_post(path: str, **kwargs) -> MagicMock:
        if "/complete" in path:
            time.sleep(0.05)  # Simulate slow upload
            events.append("complete:j1")
        elif "/block" in path:
            events.append("block:j2")
        return MagicMock()

    client.post.side_effect = tracking_post

    def process_fn(job: dict) -> dict:
        if job["job_id"] == "j2":
            raise BlockJob("wrong media type")
        return {"ok": True}

    worker = StubWorker(client=client, process_fn=process_fn, once=True)
    worker.run()

    # j1's completion must finish before j2 is blocked.
    assert events.index("complete:j1") < events.index("block:j2")


@pytest.mark.fast
def test_fail_job_waits_for_inflight() -> None:
    """When a job fails, the worker waits for any in-flight completion first."""
    events: list[str] = []

    jobs = [
        {"job_id": "j1", "rel_path": "a.jpg"},
        {"job_id": "j2", "rel_path": "b.jpg"},
    ]
    client = _mock_client_with_jobs(jobs)

    original_post = client.post

    def tracking_post(path: str, **kwargs) -> MagicMock:
        if "/complete" in path:
            time.sleep(0.05)
            events.append("complete:j1")
        elif "/fail" in path:
            events.append("fail:j2")
        return MagicMock()

    client.post.side_effect = tracking_post

    def process_fn(job: dict) -> dict:
        if job["job_id"] == "j2":
            raise RuntimeError("processing error")
        return {"ok": True}

    worker = StubWorker(client=client, process_fn=process_fn, once=True)
    worker.run()

    assert events.index("complete:j1") < events.index("fail:j2")


@pytest.mark.fast
def test_final_completion_is_awaited_on_shutdown() -> None:
    """The last job's completion must finish before run() returns."""
    completion_finished = threading.Event()

    jobs = [{"job_id": "j1", "rel_path": "a.jpg"}]
    client = _mock_client_with_jobs(jobs)

    original_post = client.post

    def slow_post(path: str, **kwargs) -> MagicMock:
        if "/complete" in path:
            time.sleep(0.1)
            completion_finished.set()
        return MagicMock()

    client.post.side_effect = slow_post

    worker = StubWorker(client=client, process_fn=lambda job: {"ok": True}, once=True)
    worker.run()

    # By the time run() returns, the completion must have finished.
    assert completion_finished.is_set()


@pytest.mark.fast
def test_single_job_works() -> None:
    """Pipelining with only one job should work correctly."""
    jobs = [{"job_id": "j1", "rel_path": "a.jpg"}]
    client = _mock_client_with_jobs(jobs)

    worker = StubWorker(client=client, process_fn=lambda job: {"result": 1}, once=True)
    worker.run()

    # complete_job should have been called for j1.
    post_calls = [c for c in client.post.call_args_list if "/complete" in str(c)]
    assert len(post_calls) == 1


@pytest.mark.fast
def test_no_jobs_available() -> None:
    """When no jobs are available, run() exits cleanly with --once."""
    client = _mock_client_with_jobs([])
    worker = StubWorker(client=client, once=True)
    worker.run()
    # No exceptions, no complete calls.
    post_calls = [c for c in client.post.call_args_list if "/complete" in str(c)]
    assert len(post_calls) == 0
