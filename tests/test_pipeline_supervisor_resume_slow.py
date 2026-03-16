"""
Regression test: pipeline supervisor should continue processing assets that are already
in proxy_ready state when restarted with --skip-scan.

Bug scenario:
  1. A prior pipeline run completed proxy (and exif) for a batch of assets and then stopped
     (e.g. CTRL-C, container restart) before _enqueue_downstream fired — or the enqueue
     subprocess itself failed silently.
  2. Assets now sit in `proxy_ready` status in the DB; no `worker_jobs` rows exist for
     `ai_vision` or `embed`.
  3. The supervisor uses `lumiverb status --output json` to decide what to do.
     That command counts *pending worker_jobs* — not asset states — so it reports
     pending=0 for every stage.
  4. Supervisor sees nothing to do → exits immediately under --once.
  5. Expected: supervisor should start `vision` and `embed` workers for these assets.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.workers.pipeline_supervisor import PipelineSupervisor


def _make_supervisor(**overrides: Any) -> PipelineSupervisor:
    mock_lock_repo = MagicMock()
    mock_lock_repo.heartbeat.return_value = None
    defaults: dict[str, Any] = dict(
        library_id="lib_test",
        library_name="test-library",
        tenant_id="tenant_test",
        client=MagicMock(),
        lock_repo=mock_lock_repo,
        media_type="image",
        once=True,
        skip_scan=True,
    )
    defaults.update(overrides)
    return PipelineSupervisor(**defaults)


@pytest.mark.fast
def test_supervisor_does_not_spawn_video_preview_for_image_media_type() -> None:
    """When media_type='image', supervisor should never spawn video-preview worker.

    This covers a safety invariant: even if the status JSON reports pending
    work for the video-preview stage (e.g. due to misclassified assets or
    stale data), an image-only pipeline run should not start video workers.
    """
    spawned_workers: list[str] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:  # noqa: ARG001
        if "worker" in cmd:
            idx = cmd.index("worker")
            spawned_workers.append(cmd[idx + 1])
        proc = MagicMock()
        proc.stdout = iter([])
        proc.returncode = 0
        proc.wait.return_value = 0
        return proc

    # Status JSON reports only a video-preview stage as pending.
    status_with_video_preview_pending: dict[str, Any] = {
        "stages": [
            {"name": "video-preview", "pending": 10, "completed": 0, "failed": 0},
        ]
    }

    def status_side_effect(library_name: str) -> dict[str, Any]:  # noqa: ARG001
        return status_with_video_preview_pending

    supervisor = _make_supervisor(media_type="image", skip_scan=True, once=True)

    with (
        patch(
            "src.workers.pipeline_supervisor._run_status_json",
            side_effect=status_side_effect,
        ),
        patch(
            "src.workers.pipeline_supervisor.subprocess.Popen",
            side_effect=fake_popen,
        ),
    ):
        supervisor.run()

    # No video workers should be spawned when running an image-only pipeline.
    assert "video-preview" not in spawned_workers
    assert "video-index" not in spawned_workers


@pytest.mark.slow
def test_supervisor_runs_vision_and_embed_for_proxy_ready_assets() -> None:
    """
    DB state being simulated
    ------------------------
    Table: assets
      asset_id   | library_id | status      | media_type
      -----------+------------+-------------+-----------
      ast_abc123 | lib_test   | proxy_ready | image/jpeg
      ...  (thousands more like this)

    Table: worker_jobs   (empty for these assets — no ai_vision/embed jobs were ever enqueued)

    What `lumiverb status --library test-library --output json` returns in this state:
      proxy    completed=N, pending=0   (jobs are done / no longer pending)
      exif     completed=N, pending=0
      ai_vision                pending=0   ← never enqueued → invisible to status
      embed                    pending=0   ← never enqueued → invisible to status

    The fix: before the main while-loop the supervisor runs a catch-up enqueue for all
    upstream stages (proxy → ai_vision + embed).  enqueue is idempotent, so this is safe.
    After that enqueue the status command would return pending=N for ai_vision and embed,
    which is what the second mock response below simulates.

    Expected behaviour
    ------------------
    The pipeline supervisor (--skip-scan --once) detects the gap, enqueues ai_vision and
    embed, then starts the corresponding workers.

    Actual behaviour (bug, now fixed)
    ----------------------------------
    Supervisor called status, saw pending=0 everywhere, concluded there was nothing to
    do, and exited immediately — vision and embed workers were never started.
    """
    # Phase 1: status before catch-up enqueue — proxy done, no downstream jobs yet.
    status_no_pending: dict[str, Any] = {
        "stages": [
            {"name": "proxy", "pending": 0, "completed": 50_000},
            {"name": "exif", "pending": 0, "completed": 50_000},
            {"name": "ai_vision", "pending": 0, "completed": 0},
            {"name": "embed", "pending": 0, "completed": 0},
            {"name": "search_sync", "pending": 0, "completed": 0},
        ]
    }

    # Phase 2: status after catch-up enqueue — ai_vision + embed jobs now exist.
    status_after_enqueue: dict[str, Any] = {
        "stages": [
            {"name": "proxy", "pending": 0, "completed": 50_000},
            {"name": "exif", "pending": 0, "completed": 50_000},
            {"name": "ai_vision", "pending": 50_000, "completed": 0},
            {"name": "embed", "pending": 50_000, "completed": 0},
            {"name": "search_sync", "pending": 0, "completed": 0},
        ]
    }

    spawned_workers: list[str] = []
    enqueued_types: list[str] = []

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        if "worker" in cmd:
            idx = cmd.index("worker")
            spawned_workers.append(cmd[idx + 1])
        proc = MagicMock()
        proc.stdout = iter([])
        proc.returncode = 0
        proc.wait.return_value = 0
        return proc

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        if "enqueue" in cmd and "--job-type" in cmd:
            idx = cmd.index("--job-type")
            enqueued_types.append(cmd[idx + 1])
        return MagicMock(returncode=0, stdout="enqueued 50000", stderr="")

    # Phase 3: after workers have run — all done, nothing pending.
    status_all_done: dict[str, Any] = {
        "stages": [
            {"name": "proxy", "pending": 0, "completed": 50_000},
            {"name": "exif", "pending": 0, "completed": 50_000},
            {"name": "ai_vision", "pending": 0, "completed": 50_000},
            {"name": "embed", "pending": 0, "completed": 50_000},
            {"name": "search_sync", "pending": 0, "completed": 0},
        ]
    }

    def status_side_effect(library_name: str) -> dict[str, Any]:  # noqa: ARG001
        # Simulate DB state transitions:
        #  1. Before catch-up enqueue: no pending downstream jobs.
        #  2. After enqueue, before workers complete: ai_vision + embed are pending.
        #  3. After workers have run: all done.
        if "vision" in spawned_workers:
            return status_all_done
        if "ai_vision" in enqueued_types and "embed" in enqueued_types:
            return status_after_enqueue
        return status_no_pending

    supervisor = _make_supervisor()

    with (
        patch(
            "src.workers.pipeline_supervisor._run_status_json",
            side_effect=status_side_effect,
        ),
        patch(
            "src.workers.pipeline_supervisor.subprocess.Popen",
            side_effect=fake_popen,
        ),
        patch(
            "src.workers.pipeline_supervisor.subprocess.run",
            side_effect=fake_run,
        ),
    ):
        supervisor.run()

    assert "ai_vision" in enqueued_types, (
        f"Expected catch-up enqueue to request 'ai_vision' jobs for proxy_ready assets; "
        f"enqueued: {enqueued_types}"
    )
    assert "embed" in enqueued_types, (
        f"Expected catch-up enqueue to request 'embed' jobs for proxy_ready assets; "
        f"enqueued: {enqueued_types}"
    )
    assert "vision" in spawned_workers, (
        f"Expected 'vision' worker to be spawned after catch-up enqueue; "
        f"workers started: {spawned_workers}"
    )
    assert "embed" in spawned_workers, (
        f"Expected 'embed' worker to be spawned after catch-up enqueue; "
        f"workers started: {spawned_workers}"
    )
