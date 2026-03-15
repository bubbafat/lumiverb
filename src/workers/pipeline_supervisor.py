"""Pipeline supervisor: orchestrates scan + worker subprocesses with lock heartbeat and optional dashboard."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.repository.tenant import PipelineLockRepository

_log = logging.getLogger(__name__)


class PathUnreachableError(Exception):
    """Raised when the library root_path is not reachable (e.g. not a directory or missing)."""

    def __init__(self, path: str, reason: str = "not a directory or unreachable") -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Path unreachable: {path} ({reason})")


@dataclass(frozen=True)
class PipelineStage:
    """Single pipeline stage: name (job_type), label, worker CLI command, media type, track."""

    name: str
    label: str
    worker_cmd: str
    media_type: str  # "image" | "video"
    track: str  # "image" | "video"


# Stage name matches status JSON "name" (job_type). worker_cmd is the CLI subcommand (e.g. vision not ai_vision).
IMAGE_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage("proxy", "Proxy", "proxy", "image", "image"),
    PipelineStage("exif", "EXIF", "exif", "image", "image"),
    PipelineStage("ai_vision", "Vision (AI)", "vision", "image", "image"),
    PipelineStage("embed", "Embeddings", "embed", "image", "image"),
    PipelineStage("search_sync", "Search Sync", "search-sync", "image", "image"),
)
VIDEO_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage("proxy", "Proxy", "proxy", "video", "video"),
    PipelineStage("video-index", "Video Index", "video-index", "video", "video"),
    PipelineStage("video-preview", "Video Preview", "video-preview", "video", "video"),
    PipelineStage("video-vision", "Video Vision", "video-vision", "video", "video"),
    PipelineStage("search_sync", "Search Sync", "search-sync", "video", "video"),
)


def _check_path_reachable(path: str) -> bool:
    """Return True if path exists and is a directory."""
    return os.path.isdir(path)


def _resolve_root_path(libraries: list[dict[str, Any]], library_name: str | None, library_id: str | None) -> str:
    """Find library in GET /v1/libraries response by name or library_id; return root_path. Raises if not found."""
    for lib in libraries:
        if (library_name and lib.get("name") == library_name) or (
            library_id and lib.get("library_id") == library_id
        ):
            rp = lib.get("root_path")
            if rp is None:
                raise ValueError(f"Library has no root_path: {lib.get('library_id')}")
            return rp
    raise ValueError(f"Library not found: name={library_name!r} library_id={library_id!r}")


def _lumiverb_cmd() -> list[str]:
    """Executable for lumiverb CLI (same Python, -m or script)."""
    return [sys.executable, "-m", "src.cli.main"]


def _run_status_json(library_name: str) -> dict[str, Any]:
    """Run lumiverb status --library {name} --output json; return parsed JSON. Raises on failure."""
    cmd = _lumiverb_cmd() + ["status", "--library", library_name, "--output", "json"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"status --output json failed: {result.stderr or result.stdout}")
    return json.loads(result.stdout)


def _all_worker_stages(media_type: str) -> list[PipelineStage]:
    """Deduplicated list of all stages for the given media_type (no pending filter)."""
    if media_type == "image":
        candidates: list[PipelineStage] = list(IMAGE_STAGES)
    elif media_type == "video":
        candidates = list(VIDEO_STAGES)
    else:
        candidates = list(IMAGE_STAGES)
        seen_cmds = {s.worker_cmd for s in candidates}
        for s in VIDEO_STAGES:
            if s.worker_cmd not in seen_cmds:
                candidates.append(s)
                seen_cmds.add(s.worker_cmd)
    seen: set[str] = set()
    out: list[PipelineStage] = []
    for s in candidates:
        if s.worker_cmd not in seen:
            seen.add(s.worker_cmd)
            out.append(s)
    return out


def _stages_with_pending(stages_payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return stages that have pending > 0 (from status JSON)."""
    return [s for s in stages_payload if s.get("pending", 0) > 0]


def _select_stages_to_run(
    stages_with_pending: list[dict[str, Any]],
    media_type: str,
) -> list[PipelineStage]:
    """
    Select pipeline stages to run: only those with pending > 0, filtered by media_type,
    deduplicated by worker_cmd (first occurrence in image then video order).
    """
    if media_type == "image":
        stage_order = IMAGE_STAGES
    elif media_type == "video":
        stage_order = VIDEO_STAGES
    else:
        # "all": image stages first, then video-only (dedupe by worker_cmd)
        stage_order = list(IMAGE_STAGES)
        seen_cmds = {s.worker_cmd for s in stage_order}
        for s in VIDEO_STAGES:
            if s.worker_cmd not in seen_cmds:
                stage_order.append(s)
                seen_cmds.add(s.worker_cmd)

    pending_names = {s["name"] for s in stages_with_pending}
    seen: set[str] = set()
    out: list[PipelineStage] = []
    for stage in stage_order:
        if stage.name not in pending_names:
            continue
        if stage.worker_cmd in seen:
            continue
        seen.add(stage.worker_cmd)
        out.append(stage)
    return out


class PipelineSupervisor:
    """
    Orchestrates scan + status poll + worker subprocesses. Assumes lock is already held by caller;
    runs a heartbeat thread and does not acquire/release lock.
    """

    def __init__(
        self,
        *,
        library_id: str,
        library_name: str,
        tenant_id: str,
        client: Any,
        lock_repo: PipelineLockRepository,
        dashboard: Any | None = None,
        media_type: str = "all",
        path_prefix: str | None = None,
        once: bool = False,
        interval: int = 60,
        lock_timeout_minutes: int = 5,
        skip_scan: bool = False,
    ) -> None:
        self.library_id = library_id
        self.library_name = library_name
        self.tenant_id = tenant_id
        self._client = client
        self._lock_repo = lock_repo
        self._dashboard = dashboard
        self._media_type = media_type
        self._path_prefix = path_prefix
        self._once = once
        self._interval = interval
        self._lock_timeout_minutes = lock_timeout_minutes
        self._skip_scan = skip_scan
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._current_proc: subprocess.Popen | None = None

    def _terminate_current_proc(self) -> None:
        """Terminate the current subprocess if any (e.g. on CTRL+C)."""
        if self._current_proc is None:
            return
        try:
            self._current_proc.terminate()
            try:
                self._current_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._current_proc.kill()
                self._current_proc.wait(timeout=5)
        except Exception as e:
            _log.warning("Error terminating subprocess: %s", e)
        finally:
            self._current_proc = None

    def _dashboard_update(self, stages: list[dict[str, Any]], log_line: str | None = None) -> None:
        if self._dashboard is None:
            return
        update = getattr(self._dashboard, "update", None)
        if callable(update):
            update(stages, log_line)

    def _dashboard_set_worker_status(self, worker_cmd: str, label: str, status: str) -> None:
        if self._dashboard is None:
            return
        fn = getattr(self._dashboard, "set_worker_status", None)
        if callable(fn):
            fn(worker_cmd, label, status)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(30):
            try:
                self._lock_repo.heartbeat(self.tenant_id)
            except Exception as e:
                _log.warning("Pipeline heartbeat failed: %s", e)

    def _run_scan(self) -> None:
        cmd = _lumiverb_cmd() + ["scan", "--library", self.library_name]
        if self._path_prefix:
            cmd += ["--path", self._path_prefix]
        _log.info("Running scan: %s", " ".join(cmd))
        self._dashboard_set_worker_status("scan", "Scan", "active")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._current_proc = proc
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                self._dashboard_update([], log_line=line)
            proc.wait()
            if proc.returncode != 0:
                _log.warning("Scan exited with code %s", proc.returncode)
                self._dashboard_set_worker_status("scan", "Scan", "error")
            else:
                self._dashboard_set_worker_status("scan", "Scan", "completed")
        finally:
            self._current_proc = None

    def _run_workers(self, stages: list[PipelineStage]) -> None:
        for stage in stages:
            cmd = _lumiverb_cmd() + [
                "worker",
                stage.worker_cmd,
                "--library",
                self.library_name,
                "--once",
            ]
            if self._path_prefix:
                cmd += ["--path", self._path_prefix]
            _log.info("Running worker: %s", " ".join(cmd))
            self._dashboard_set_worker_status(stage.worker_cmd, stage.label, "active")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self._current_proc = proc
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    self._dashboard_update([], log_line=line)
                proc.wait()
                if proc.returncode != 0:
                    _log.warning("Worker %s exited with code %s", stage.worker_cmd, proc.returncode)
                    self._dashboard_set_worker_status(stage.worker_cmd, stage.label, "error")
                else:
                    self._dashboard_set_worker_status(stage.worker_cmd, stage.label, "idle")
            finally:
                self._current_proc = None

    def run(self) -> None:
        """
        Run the pipeline loop. Caller must hold the lock and will release it in finally.
        Starts heartbeat thread, optionally runs scan, then polls status and spawns workers until
        no pending and (once mode or interrupted).
        """
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

        # Pre-populate worker status as idle so the dashboard shows the full picture immediately.
        if not self._skip_scan:
            self._dashboard_set_worker_status("scan", "Scan", "idle")
        for stage in _all_worker_stages(self._media_type):
            self._dashboard_set_worker_status(stage.worker_cmd, stage.label, "idle")

        try:
            if not self._skip_scan:
                libraries = self._client.get("/v1/libraries").json()
                root_path = _resolve_root_path(
                    libraries,
                    self.library_name,
                    self.library_id,
                )
                if not _check_path_reachable(root_path):
                    raise PathUnreachableError(root_path)
                self._run_scan()

            while True:
                try:
                    data = _run_status_json(self.library_name)
                except Exception as e:
                    _log.warning("Status poll failed: %s", e)
                    self._dashboard_update(
                        [],
                        log_line=f"Status poll failed: {e}",
                    )
                    # Keep last known state; sleep and retry
                    self._heartbeat_stop.wait(min(60, self._interval))
                    continue

                stages_payload = data.get("stages", [])
                self._dashboard_update(stages_payload, log_line=None)

                pending = _stages_with_pending(stages_payload)
                if not pending:
                    if self._once:
                        return
                    self._heartbeat_stop.wait(self._interval)
                    continue

                to_run = _select_stages_to_run(pending, self._media_type)
                if not to_run:
                    self._heartbeat_stop.wait(min(10, self._interval))
                    continue

                self._run_workers(to_run)
                # Loop immediately to re-poll (no sleep) so we pick up new pending work
        finally:
            self._terminate_current_proc()
            self._heartbeat_stop.set()
            if self._heartbeat_thread is not None:
                self._heartbeat_thread.join(timeout=35)
            self._heartbeat_thread = None
