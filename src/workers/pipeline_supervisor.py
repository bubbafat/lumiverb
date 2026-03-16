"""Pipeline supervisor: orchestrates scan + worker subprocesses with lock heartbeat and optional dashboard."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.repository.tenant import PipelineLockRepository

_log = logging.getLogger(__name__)

# Maps completed stage name → job types to enqueue next.
# search_sync is populated via the search_sync_queue by the ai_vision/video-vision
# completion handlers on the API side, so it doesn't need explicit supervisor enqueuing.
_DOWNSTREAM: dict[str, list[str]] = {
    "proxy": ["ai_vision", "embed", "video-preview"],
    "video-index": ["video-vision"],
}


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
    """Resolve the lumiverb entry-point script alongside the running Python executable."""
    return [str(pathlib.Path(sys.executable).parent / "lumiverb")]


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
        raise RuntimeError(f"status --output json failed (rc={result.returncode}): {result.stderr or result.stdout}")
    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(f"status --output json produced no output; stderr: {result.stderr.strip()!r}")
    return json.loads(stdout)


def _run_status_json_tenant() -> dict[str, Any]:
    """Run lumiverb status --output json (no library); return parsed JSON. Raises on failure."""
    cmd = _lumiverb_cmd() + ["status", "--output", "json"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"status --output json failed (rc={result.returncode}): {result.stderr or result.stdout}")
    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(f"status --output json produced no output; stderr: {result.stderr.strip()!r}")
    return json.loads(stdout)


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
        retry_failures: bool = False,
        libraries: list[dict[str, Any]] | None = None,
    ) -> None:
        # libraries: tenant-wide mode — list of {library_id, library_name, root_path}
        self._libraries = libraries
        self.library_id = library_id
        self.library_name = library_name
        self.tenant_id = tenant_id
        self._client = client
        self._lock_repo = lock_repo
        self._dashboard = dashboard
        self._log_file_path: str | None = None
        self._media_type = media_type
        self._path_prefix = path_prefix
        self._once = once
        self._interval = interval
        self._lock_timeout_minutes = lock_timeout_minutes
        self._skip_scan = skip_scan
        self._retry_failures = retry_failures
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._status_poll_thread: threading.Thread | None = None
        self._current_procs: list[subprocess.Popen] = []
        self._procs_lock = threading.Lock()
        self._log_lock = threading.Lock()

    def set_log_file(self, path: str | None) -> None:
        """Configure optional log file path for pipeline output."""
        self._log_file_path = path

    def _write_log_line(self, line: str) -> None:
        """Append a single log line to the configured log file, if any."""
        if not self._log_file_path:
            return
        try:
            with self._log_lock:
                with open(self._log_file_path, "a", encoding="utf-8") as f:
                    f.write(line.rstrip("\n") + "\n")
        except Exception:
            # Logging failures should never crash the supervisor; they are best-effort.
            _log.debug("Failed to write pipeline log line", exc_info=True)

    def _terminate_current_proc(self) -> None:
        """Terminate all running subprocesses (e.g. on CTRL+C)."""
        with self._procs_lock:
            procs = list(self._current_procs)
        for proc in procs:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception as e:
                _log.warning("Error terminating subprocess: %s", e)
        with self._procs_lock:
            self._current_procs.clear()

    def _dashboard_update(self, stages: list[dict[str, Any]], log_line: str | None = None, workers: int = 0) -> None:
        if log_line is not None:
            self._write_log_line(log_line)
        if self._dashboard is None:
            return
        update = getattr(self._dashboard, "update", None)
        if callable(update):
            update(stages, None, workers)

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

    def _fetch_stages(self) -> list[dict[str, Any]]:
        """Fetch current stage counts and return as a flat list suitable for dashboard.update()."""
        if self._libraries is not None:
            data = _run_status_json_tenant()
            flat: list[dict[str, Any]] = []
            for lib_data in data.get("libraries", []):
                lib_name = lib_data.get("library", "")
                for stage in lib_data.get("stages", []):
                    flat.append({**stage, "library_name": lib_name})
            return flat
        else:
            data = _run_status_json(self.library_name)
            return data.get("stages", [])

    def _status_poll_loop(self) -> None:
        """Background thread: refresh dashboard stage counts every 10s while workers are running."""
        while not self._heartbeat_stop.wait(10):
            try:
                stages = self._fetch_stages()
                self._dashboard_update(stages)
            except Exception as e:
                _log.debug("Background status poll failed: %s", e)

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
        with self._procs_lock:
            self._current_procs.append(proc)
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
            with self._procs_lock:
                if proc in self._current_procs:
                    self._current_procs.remove(proc)

    def _retry_failed_jobs(self, library_names: list[str]) -> None:
        """
        Re-enqueue failed (non-blocked) jobs for all stages and the given libraries.
        Blocked jobs are skipped — they require --force to reset.
        """
        all_stages = _all_worker_stages(self._media_type)
        for stage in all_stages:
            for lib_name in library_names:
                cmd = _lumiverb_cmd() + [
                    "enqueue",
                    "--job-type", stage.name,
                    "--library", lib_name,
                    "--retry-failed",
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    _log.warning(
                        "retry-failed enqueue for %s/%s failed: %s",
                        stage.name, lib_name, result.stderr or result.stdout,
                    )
                else:
                    out = result.stdout.strip()
                    if out:
                        self._dashboard_update([], log_line=f"[Retry] {out}")
                    _log.debug("retry-failed %s/%s: %s", stage.name, lib_name, out)

    def _run_workers(self, stages: list[PipelineStage]) -> None:
        """Spawn all stages concurrently; each drains stdout in its own thread."""
        self._run_workers_concurrent(stages, library_name=self.library_name, path_prefix=self._path_prefix)

    def _run_workers_tenant(self, stages: list[PipelineStage]) -> None:
        """Like _run_workers but without --library (workers claim from all libraries)."""
        self._run_workers_concurrent(stages, library_name=None, path_prefix=None)

    def _enqueue_downstream(self, completed: list[PipelineStage], library_names: list[str]) -> None:
        """
        After a batch of workers completes, enqueue any job types that depend on them.
        library_names: list of library names to enqueue for (one entry in single-lib mode).
        """
        to_enqueue: list[str] = []
        seen: set[str] = set()
        for stage in completed:
            for job_type in _DOWNSTREAM.get(stage.name, []):
                if job_type not in seen:
                    seen.add(job_type)
                    to_enqueue.append(job_type)
        if not to_enqueue:
            return
        for job_type in to_enqueue:
            for lib_name in library_names:
                cmd = _lumiverb_cmd() + ["enqueue", "--job-type", job_type, "--library", lib_name]
                self._dashboard_update([], log_line=f"Enqueueing {job_type} for {lib_name}...")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    _log.warning("Enqueue %s failed: %s", job_type, result.stderr or result.stdout)
                else:
                    _log.debug("Enqueued %s for %s: %s", job_type, lib_name, result.stdout.strip())

    def _run_workers_concurrent(
        self,
        stages: list[PipelineStage],
        *,
        library_name: str | None,
        path_prefix: str | None,
    ) -> None:
        """Spawn all stages in parallel; wait for all to finish."""
        if not stages:
            return

        def _spawn(stage: PipelineStage) -> tuple[PipelineStage, subprocess.Popen]:
            cmd = _lumiverb_cmd() + ["worker", stage.worker_cmd, "--once"]
            if library_name:
                cmd += ["--library", library_name]
            # All workers must respect optional --path scoping; each worker
            # is responsible for interpreting the prefix appropriately.
            if path_prefix:
                cmd += ["--path", path_prefix]
            _log.info("Running worker: %s", " ".join(cmd))
            self._dashboard_set_worker_status(stage.worker_cmd, stage.label, "active")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            with self._procs_lock:
                self._current_procs.append(proc)
            return stage, proc

        def _drain(stage: PipelineStage, proc: subprocess.Popen) -> None:
            assert proc.stdout is not None
            prefix = f"[{stage.label}] "
            for line in proc.stdout:
                self._dashboard_update([], log_line=prefix + line.rstrip("\n"))
            proc.wait()
            with self._procs_lock:
                if proc in self._current_procs:
                    self._current_procs.remove(proc)
            if proc.returncode != 0:
                _log.warning("Worker %s exited with code %s", stage.worker_cmd, proc.returncode)
                self._dashboard_set_worker_status(stage.worker_cmd, stage.label, "error")
            else:
                self._dashboard_set_worker_status(stage.worker_cmd, stage.label, "idle")

        spawned = [_spawn(stage) for stage in stages]
        drain_threads = [
            threading.Thread(target=_drain, args=(stage, proc), daemon=True)
            for stage, proc in spawned
        ]
        for t in drain_threads:
            t.start()
        for t in drain_threads:
            t.join()

    def run(self) -> None:
        """
        Run the pipeline loop. Caller must hold the lock and will release it in finally.
        Starts heartbeat thread, optionally runs scan, then polls status and spawns workers until
        no pending and (once mode or interrupted).
        """
        if self._libraries is not None:
            self._run_tenant()
        else:
            self._run_single_library()

    def _run_single_library(self) -> None:
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        self._status_poll_thread = threading.Thread(target=self._status_poll_loop, daemon=True)
        self._status_poll_thread.start()

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

            # Catch-up: enqueue downstream jobs for assets that completed a prior stage
            # in a previous run but whose downstream jobs were never queued (e.g. after
            # a crash or restart).  enqueue is idempotent — it skips assets that already
            # have a pending/claimed job — so this is always safe to run.
            catchup_stages = [s for s in _all_worker_stages(self._media_type) if s.name in _DOWNSTREAM]
            if catchup_stages:
                self._enqueue_downstream(catchup_stages, library_names=[self.library_name])

            if self._retry_failures:
                self._retry_failed_jobs(library_names=[self.library_name])

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
                self._dashboard_update(stages_payload, log_line=None, workers=data.get("workers", 0))

                pending = _stages_with_pending(stages_payload)
                if not pending:
                    if self._once:
                        return
                    self._heartbeat_stop.wait(self._interval)
                    continue

                to_run = _select_stages_to_run(pending, self._media_type)
                if not to_run:
                    # Pending work exists but none matches this media_type filter.
                    if self._once:
                        return
                    self._heartbeat_stop.wait(min(10, self._interval))
                    continue

                self._run_workers(to_run)
                self._enqueue_downstream(to_run, library_names=[self.library_name])
                # Loop immediately to re-poll (no sleep) so we pick up new pending work
        finally:
            self._terminate_current_proc()
            self._heartbeat_stop.set()
            if self._heartbeat_thread is not None:
                self._heartbeat_thread.join(timeout=35)
            self._heartbeat_thread = None
            if self._status_poll_thread is not None:
                self._status_poll_thread.join(timeout=15)
            self._status_poll_thread = None

    def _run_tenant(self) -> None:
        """Tenant-wide pipeline loop: scan all libraries, poll status across all, run workers without --library."""
        assert self._libraries is not None
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        self._status_poll_thread = threading.Thread(target=self._status_poll_loop, daemon=True)
        self._status_poll_thread.start()

        if not self._skip_scan:
            self._dashboard_set_worker_status("scan", "Scan", "idle")
        for stage in _all_worker_stages(self._media_type):
            self._dashboard_set_worker_status(stage.worker_cmd, stage.label, "idle")

        try:
            if not self._skip_scan:
                for lib in self._libraries:
                    root_path = lib.get("root_path", "")
                    if not _check_path_reachable(root_path):
                        raise PathUnreachableError(root_path)
                for lib in self._libraries:
                    cmd = _lumiverb_cmd() + ["scan", "--library", lib["library_name"]]
                    _log.info("Running scan: %s", " ".join(cmd))
                    self._dashboard_set_worker_status("scan", f"Scan ({lib['library_name']})", "active")
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    with self._procs_lock:
                        self._current_procs.append(proc)
                    try:
                        assert proc.stdout is not None
                        for line in proc.stdout:
                            self._dashboard_update([], log_line=line.rstrip("\n"))
                        proc.wait()
                        if proc.returncode != 0:
                            _log.warning("Scan exited with code %s", proc.returncode)
                            self._dashboard_set_worker_status("scan", f"Scan ({lib['library_name']})", "error")
                        else:
                            self._dashboard_set_worker_status("scan", f"Scan ({lib['library_name']})", "completed")
                    finally:
                        with self._procs_lock:
                            if proc in self._current_procs:
                                self._current_procs.remove(proc)

            # Catch-up enqueue (same rationale as single-library mode above).
            catchup_stages = [s for s in _all_worker_stages(self._media_type) if s.name in _DOWNSTREAM]
            lib_names = [lib["library_name"] for lib in self._libraries]
            if catchup_stages:
                self._enqueue_downstream(catchup_stages, library_names=lib_names)

            if self._retry_failures:
                self._retry_failed_jobs(library_names=lib_names)

            while True:
                try:
                    data = _run_status_json_tenant()
                except Exception as e:
                    _log.warning("Status poll failed: %s", e)
                    self._dashboard_update([], log_line=f"Status poll failed: {e}")
                    self._heartbeat_stop.wait(min(60, self._interval))
                    continue

                # Flatten per-library stages for dashboard; aggregate pending names for worker selection
                flat_stages: list[dict[str, Any]] = []
                pending_names: set[str] = set()
                for lib_data in data.get("libraries", []):
                    lib_name = lib_data.get("library", "")
                    for stage in lib_data.get("stages", []):
                        flat_stages.append({**stage, "library_name": lib_name})
                        if stage.get("pending", 0) > 0:
                            pending_names.add(stage["name"])

                self._dashboard_update(flat_stages, log_line=None, workers=data.get("workers", 0))

                if not pending_names:
                    if self._once:
                        return
                    self._heartbeat_stop.wait(self._interval)
                    continue

                mock_pending = [{"name": n} for n in pending_names]
                to_run = _select_stages_to_run(mock_pending, self._media_type)
                if not to_run:
                    # Pending work exists but none matches this media_type filter.
                    if self._once:
                        return
                    self._heartbeat_stop.wait(min(10, self._interval))
                    continue

                self._run_workers_tenant(to_run)
                lib_names = [lib["library_name"] for lib in self._libraries]
                self._enqueue_downstream(to_run, library_names=lib_names)
        finally:
            self._terminate_current_proc()
            self._heartbeat_stop.set()
            if self._heartbeat_thread is not None:
                self._heartbeat_thread.join(timeout=35)
            self._heartbeat_thread = None
            if self._status_poll_thread is not None:
                self._status_poll_thread.join(timeout=15)
            self._status_poll_thread = None
