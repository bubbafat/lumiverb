"""The brain's worker: scan what changed, enrich what's missing (ADR-016 phase 2).

Runs as a service (`lumiverb worker`). Each cycle, for each library:

1. If its storage is reachable from here and the Mac reported changes (or a
   full scan is due), scan the one folder that covers them, then acknowledge
   the changes the scan saw. A file that fails to scan doesn't hold its
   changes back; it's tried again on its own, after 5 minutes, then 10, 20
   and so on up to a day.
2. If anything is missing, enrich. Videos are enriched from analysis
   proxies, so this continues while the storage sleeps; only probing and
   rendering wait for it. Enrichment runs again only when a library's
   counts change, its storage comes back, or an hour has passed, so a
   file that fails every time isn't retried every minute. Steps that need
   vision AI are skipped while none is configured.

A library is skipped, never failed: the next cycle tries again.
"""

from __future__ import annotations

import fcntl
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from rich.console import Console

from src.client.cache_dir import cache_dir
from src.client.cli.client import LumiverbClient
from src.client.cli.roots import reachable_root
from src.shared.io_utils import resolve_source_path

if TYPE_CHECKING:
    from src.client.cli.scan import ScanStats

logger = logging.getLogger(__name__)

DEFAULT_POLL_SEC = 60.0
DEFAULT_FULL_SCAN_EVERY_SEC = 24 * 3600.0
DEFAULT_RETRY_EVERY_SEC = 3600.0
# Files that failed to scan are tried again after this, doubling up to a day.
RETRY_FIRST_SEC = 300.0
RETRY_MAX_SEC = 24 * 3600.0

# Enrich steps that need a vision AI endpoint, and the count each repairs.
VISION_STEPS = {"vision": "missing_vision", "ocr": "missing_ocr", "scene-vision": "missing_scene_vision"}

# repair-summary counts that enrich acts on. Counts like missing_proxy are
# scan's job.
ENRICH_COUNTS = (
    "missing_probe",
    "missing_analysis_proxy",
    "missing_embeddings",
    "missing_vision",
    "missing_faces",
    "missing_ocr",
    "missing_transcription",
    "missing_video_scenes",
    "missing_scene_vision",
    "stale_search_sync",
)


def scan_scope(rel_paths: list[str], is_dir: Callable[[str], bool]) -> str | None:
    """The one folder whose scan covers every change; None means the whole library.

    A changed folder is scanned itself; a changed or removed file, or a
    removed folder, by its parent. Several changes share their deepest
    common folder, so a file moved between two of them is seen as a move.
    """
    folders: list[tuple[str, ...]] = []
    for rel in rel_paths:
        parts = PurePosixPath(rel).parts if rel else ()
        if parts and not is_dir(rel):
            parts = parts[:-1]
        folders.append(parts)
    if not folders:
        return None
    common = folders[0]
    for parts in folders[1:]:
        n = 0
        while n < min(len(common), len(parts)) and common[n] == parts[n]:
            n += 1
        common = common[:n]
    return "/".join(common) or None


def _within(rel: str, folder: str | None) -> bool:
    """rel is the folder or inside it. None or "" is the whole library."""
    return not folder or rel == folder or rel.startswith(folder + "/")


@dataclass
class Retry:
    """Files a library's scans couldn't take in, and when to try them again."""

    paths: set[str]
    delay: float
    due: float


@dataclass
class WorkerState:
    """What the worker remembers between cycles (in memory only)."""

    last_full_scan: dict[str, float] = field(default_factory=dict)
    # library_id -> (what the library looked like after the last enrich, when)
    last_enrich: dict[str, tuple[tuple, float]] = field(default_factory=dict)
    retries: dict[str, Retry] = field(default_factory=dict)


class WorkerLock:
    """One worker per machine: two would scan and render the same files."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or cache_dir("worker.lock")
        self._fd: int | None = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def _vision_configured(client: LumiverbClient) -> bool:
    from src.client.cli.ingest import _resolve_vision_config

    try:
        url, _key, model, _source = _resolve_vision_config(client)
    except Exception:  # noqa: BLE001 — e.g. the endpoint is down; its steps would fail anyway
        logger.warning("worker: couldn't resolve the vision AI endpoint; skipping its steps this cycle")
        return False
    return bool(url and model)


def _fingerprint(summary: dict, reachable: bool, skip: set[str]) -> tuple:
    skipped = {VISION_STEPS[s] for s in skip}
    return (reachable, tuple(summary.get(k, 0) for k in ENRICH_COUNTS if k not in skipped))


def _scan_library(
    client: LumiverbClient,
    library: dict,
    root: Path,
    state: WorkerState,
    *,
    now: float,
    full_scan_every: float,
    scan_fn: Callable,
    console: Console,
) -> None:
    library_id = library["library_id"]
    resp = client.get(f"/v1/libraries/{library_id}/changes").json()
    changes: list[dict] = resp.get("changes", [])
    retry = state.retries.get(library_id)
    retry_due = retry is not None and now >= retry.due
    full_due = now - state.last_full_scan.get(library_id, float("-inf")) >= full_scan_every
    if not changes and not retry_due and not full_due:
        return

    if full_due or resp.get("truncated"):
        prefix = None
    else:
        paths = [c["rel_path"] for c in changes] + (sorted(retry.paths) if retry_due else [])
        prefix = scan_scope(paths, lambda rel: resolve_source_path(root, rel).is_dir())
    logger.info(
        "worker: scanning %s%s (%d reported change(s)%s%s)",
        library["name"], f" / {prefix}" if prefix else "", len(changes),
        ", retrying failed files" if retry_due else "", ", full scan due" if full_due else "",
    )
    stats = scan_fn(client, library, path_prefix=prefix, allow_moves=True, console=console)
    if stats.root_unreachable:
        logger.warning("worker: %s became unreachable during the scan; changes kept", library["name"])
        return
    if prefix is None:
        state.last_full_scan[library_id] = now
    _note_failures(state, library, prefix, stats, now)

    # A file still being written is scanned next cycle: changes that cover
    # it wait, the rest are done.
    seen = [c for c in changes if not any(_within(s, c["rel_path"]) for s in stats.settling_paths)]
    if len(seen) < len(changes):
        logger.info("worker: in %s, %d file(s) still being written; %d change(s) wait for the next cycle",
                    library["name"], stats.settling, len(changes) - len(seen))
    if seen:
        client.post(
            f"/v1/libraries/{library_id}/changes/ack",
            json={"changes": [{"change_id": c["change_id"], "version": c["version"]} for c in seen]},
        )


def _note_failures(state: WorkerState, library: dict, prefix: str | None, stats: ScanStats, now: float) -> None:
    """Remember files that failed to scan, to try them again with back-off.

    A scan covering a failed file clears it unless it fails again.
    """
    library_id = library["library_id"]
    failed = set(stats.failed_paths)
    if stats.failed and not failed:
        failed.add(prefix or "")
    old = state.retries.pop(library_id, None)
    left = {p for p in old.paths if not _within(p, prefix)} if old else set()
    if failed:
        delay = min(old.delay * 2, RETRY_MAX_SEC) if old else RETRY_FIRST_SEC
        state.retries[library_id] = Retry(left | failed, delay, now + delay)
        logger.warning("worker: in %s, %d file(s) failed to scan; trying them again in %.0f min",
                       library["name"], len(failed), delay / 60)
    elif left:
        state.retries[library_id] = Retry(left, old.delay, old.due)


def run_cycle(
    client: LumiverbClient,
    *,
    state: WorkerState,
    console: Console,
    now: float | None = None,
    full_scan_every: float = DEFAULT_FULL_SCAN_EVERY_SEC,
    retry_every: float = DEFAULT_RETRY_EVERY_SEC,
    only: list[str] | None = None,
    scan_fn: Callable | None = None,
    enrich_fn: Callable | None = None,
) -> None:
    """Scan and enrich every library once."""
    if scan_fn is None:
        from src.client.cli.scan import run_scan as scan_fn
    if enrich_fn is None:
        from src.client.cli.repair import run_repair as enrich_fn
    now = time.time() if now is None else now

    libraries = client.get("/v1/libraries").json()
    if only:
        libraries = [lib for lib in libraries if lib["name"] in only]
    skip = set() if _vision_configured(client) else set(VISION_STEPS)

    for library in libraries:
        name = library["name"]
        # require_entries: an unmounted mount point is an empty folder, and
        # scanning it would mark every file missing.
        root = reachable_root(library, require_entries=True)
        if root is None:
            logger.info("worker: %s isn't reachable from here; not scanning it", name)
        else:
            try:
                _scan_library(client, library, root, state, now=now, full_scan_every=full_scan_every,
                              scan_fn=scan_fn, console=console)
            except Exception:  # noqa: BLE001 — one library's trouble doesn't stop the rest
                logger.exception("worker: scanning %s failed; trying again next cycle", name)

        try:
            _enrich_library(client, library, state, reachable=root is not None, skip=skip, now=now,
                            retry_every=retry_every, enrich_fn=enrich_fn, console=console)
        except Exception:  # noqa: BLE001
            logger.exception("worker: enriching %s failed; trying again next cycle", name)


def _enrich_library(
    client: LumiverbClient,
    library: dict,
    state: WorkerState,
    *,
    reachable: bool,
    skip: set[str],
    now: float,
    retry_every: float,
    enrich_fn: Callable,
    console: Console,
) -> None:
    library_id = library["library_id"]

    def look() -> tuple:
        summary = client.get("/v1/assets/repair-summary", params={"library_id": library_id}).json()
        return _fingerprint(summary, reachable, skip)

    before = look()
    if not any(before[1]):
        return
    previous = state.last_enrich.get(library_id)
    if previous is not None and previous[0] == before and now - previous[1] < retry_every:
        return  # nothing changed since the last try
    enrich_fn(client, library, job_type="all", console=console, skip_types=skip)
    state.last_enrich[library_id] = (look(), now)


def run_forever(
    *,
    poll: float = DEFAULT_POLL_SEC,
    full_scan_every: float = DEFAULT_FULL_SCAN_EVERY_SEC,
    only: list[str] | None = None,
    once: bool = False,
    console: Console | None = None,
    stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    console = console or Console(force_terminal=False, width=120)
    state = WorkerState()
    client = LumiverbClient()
    while True:
        try:
            run_cycle(client, state=state, console=console, full_scan_every=full_scan_every, only=only)
        except Exception:  # noqa: BLE001 — e.g. the API is restarting
            logger.exception("worker: cycle failed; trying again in %.0fs", poll)
        if once or stop():
            return
        sleep(poll)
        if stop():
            return
