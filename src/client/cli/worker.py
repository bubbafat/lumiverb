"""The brain's worker: scan what changed, enrich what's missing (ADR-016 phase 2).

Runs as a service (`lumiverb worker`). Each cycle scans every library, then
enriches them:

1. If a library's storage is reachable from here and the Mac reported changes (or a
   full scan is due), scan the one folder that covers them, then acknowledge
   the changes the scan saw. A file that fails to scan doesn't hold its
   changes back; it's tried again on its own, after 5 minutes, then 10, 20
   and so on up to a day. A folder that can't be listed (no permission, or
   the CIFS mount timing out) keeps the changes, which wait for the same
   back-off, and nothing is taken for deleted.
2. If anything is missing, enrich. Videos are enriched from analysis
   proxies, so this continues while the storage sleeps; only probing and
   rendering wait for it. Enrichment runs again only when a library's
   counts change, its storage comes back, or an hour has passed, so a
   file that fails every time isn't retried every minute. Steps that need
   vision AI are skipped while none is configured. Enrichment gets 15
   minutes a cycle (a first ingest's can take days), stopping between
   items so change reports are scanned next cycle; it goes on from there,
   least recently enriched library first. An item enrichment took isn't
   taken again for an hour, so one that keeps failing (a render that runs
   for 15 minutes, then fails) doesn't sit first in every cycle's queue.

A library is skipped, never failed: the next cycle tries again.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from stat import S_ISDIR
from typing import TYPE_CHECKING

from rich.console import Console

from src.client.cache_dir import cache_dir
from src.client.cli.client import LumiverbClient
from src.client.cli.roots import reachable_root
from src.client.proxy.analysis_cache import clear_leftovers
from src.shared.io_utils import is_within, resolve_source_path, stat_if_present

if TYPE_CHECKING:
    from src.client.cli.scan import ScanStats

logger = logging.getLogger(__name__)

DEFAULT_POLL_SEC = 60.0
DEFAULT_FULL_SCAN_EVERY_SEC = 24 * 3600.0
DEFAULT_RETRY_EVERY_SEC = 3600.0
# Files that failed to scan are tried again after this, doubling up to a day.
RETRY_FIRST_SEC = 300.0
RETRY_MAX_SEC = 24 * 3600.0
DEFAULT_ENRICH_BUDGET_SEC = 15 * 60.0
# A library isn't started with less time left than this: it would run out
# getting going.
ENRICH_MIN_START_SEC = 60.0

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


@dataclass
class Retry:
    """Files and folders a library's scans couldn't take in, and when to try again."""

    paths: set[str]
    delay: float
    due: float
    # Changes kept un-acked meanwhile (change_id -> version): they wait for
    # the retry instead of being rescanned every cycle.
    held: dict[str, int] = field(default_factory=dict)


@dataclass
class WorkerState:
    """What the worker remembers between cycles. Full-scan times and retries
    outlive the process (save_state), so a restart doesn't rescan everything,
    nor forget what's waiting to be tried again."""

    last_full_scan: dict[str, float] = field(default_factory=dict)
    # library_id -> (what the library looked like after the last enrich, when).
    # None: enrichment was cut short, so it goes on next cycle.
    last_enrich: dict[str, tuple[tuple | None, float]] = field(default_factory=dict)
    retries: dict[str, Retry] = field(default_factory=dict)
    # library_id -> {(step, asset_id): when enrichment may take it again}. One
    # still missing by then failed; meanwhile the rest of the step goes on.
    taken: dict[str, dict[tuple[str, str], float]] = field(default_factory=dict)


def load_state(path: Path) -> WorkerState:
    """Full-scan times and retries as an earlier run saved them; each empty if unreadable."""
    state = WorkerState()
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        return state
    unreadable = (TypeError, KeyError, AttributeError, ValueError)
    with contextlib.suppress(*unreadable):
        state.last_full_scan = {str(lib): float(when) for lib, when in saved["last_full_scan"].items()}
    with contextlib.suppress(*unreadable):
        state.retries = {
            str(lib): Retry({str(p) for p in r["paths"]}, float(r["delay"]), float(r["due"]),
                            {str(c): int(v) for c, v in r["held"].items()})
            for lib, r in saved.get("retries", {}).items()
        }
    return state


def _saved_form(state: WorkerState) -> dict:
    return {
        "last_full_scan": dict(state.last_full_scan),
        "retries": {lib: {"paths": sorted(r.paths), "delay": r.delay, "due": r.due, "held": dict(r.held)}
                    for lib, r in state.retries.items()},
    }


def save_state(path: Path, saved: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        part.write_text(json.dumps(saved))
        part.replace(path)
    except OSError as exc:
        logger.warning("worker: couldn't save its state to %s: %s", path, exc)


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


def _is_dir(root: Path, rel: str) -> bool:
    """False when it can't be checked: its parent's scan covers it either way."""
    try:
        st = stat_if_present(resolve_source_path(root, rel))
    except OSError:
        return False
    return st is not None and S_ISDIR(st.st_mode)


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
    if retry is not None and not retry_due:
        changes = [c for c in changes if retry.held.get(c["change_id"]) != c["version"]]
    full_due = now - state.last_full_scan.get(library_id, float("-inf")) >= full_scan_every
    if not changes and not retry_due and not full_due:
        return

    if full_due or resp.get("truncated"):
        prefix = None
    else:
        paths = [c["rel_path"] for c in changes] + (sorted(retry.paths) if retry_due else [])
        prefix = scan_scope(paths, lambda rel: _is_dir(root, rel))
    logger.info(
        "worker: scanning %s%s (%d reported change(s)%s%s)",
        library["name"], f" / {prefix}" if prefix else "", len(changes),
        ", retrying what failed" if retry_due else "", ", full scan due" if full_due else "",
    )
    stats = scan_fn(client, library, path_prefix=prefix, allow_moves=True, console=console)
    if stats.root_unreachable:
        logger.warning("worker: %s became unreachable during the scan; changes kept", library["name"])
        return
    if prefix is None:
        state.last_full_scan[library_id] = now
    _note_failures(state, library, prefix, stats, changes, now)
    if stats.unlisted:
        logger.warning("worker: in %s, %d folder(s) couldn't be listed (%s); changes kept for the retry",
                       library["name"], len(stats.unlisted), ", ".join(stats.unlisted[:5]) or "/")
        return

    # A file still being written is scanned next cycle: changes that cover
    # it wait, the rest are done.
    seen = [c for c in changes if not any(is_within(s, c["rel_path"]) for s in stats.settling_paths)]
    if len(seen) < len(changes):
        logger.info("worker: in %s, %d file(s) still being written; %d change(s) wait for the next cycle",
                    library["name"], stats.settling, len(changes) - len(seen))
    if seen:
        client.post(
            f"/v1/libraries/{library_id}/changes/ack",
            json={"changes": [{"change_id": c["change_id"], "version": c["version"]} for c in seen]},
        )


def _note_failures(
    state: WorkerState, library: dict, prefix: str | None, stats: ScanStats, changes: list[dict], now: float,
) -> None:
    """Remember files that failed to scan and folders that couldn't be listed,
    to try them again with back-off. A folder that couldn't be listed also
    holds back the changes the scan covered.

    A scan covering a path clears it unless it fails again.
    """
    library_id = library["library_id"]
    failed = set(stats.failed_paths) | set(stats.unlisted)
    if stats.failed and not stats.failed_paths:
        failed.add(prefix or "")
    old = state.retries.pop(library_id, None)
    scanned = {c["change_id"] for c in changes}
    left = {p for p in old.paths if not is_within(p, prefix)} if old else set()
    held = {cid: v for cid, v in old.held.items() if cid not in scanned} if old else {}
    if stats.unlisted:
        held |= {c["change_id"]: c["version"] for c in changes}
    if failed:
        delay = min(old.delay * 2, RETRY_MAX_SEC) if old else RETRY_FIRST_SEC
        state.retries[library_id] = Retry(left | failed, delay, now + delay, held)
        logger.warning("worker: in %s, %d file(s) or folder(s) failed to scan; trying again in %.0f min",
                       library["name"], len(failed), delay / 60)
    elif old and (left or held):
        state.retries[library_id] = Retry(left, old.delay, old.due, held)


def run_cycle(
    client: LumiverbClient,
    *,
    state: WorkerState,
    console: Console,
    now: float | None = None,
    full_scan_every: float = DEFAULT_FULL_SCAN_EVERY_SEC,
    retry_every: float = DEFAULT_RETRY_EVERY_SEC,
    enrich_budget: float = DEFAULT_ENRICH_BUDGET_SEC,
    only: list[str] | None = None,
    scan_fn: Callable | None = None,
    enrich_fn: Callable | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Scan every library, then enrich them for up to enrich_budget seconds."""
    if scan_fn is None:
        from src.client.cli.scan import run_scan as scan_fn
    if enrich_fn is None:
        from src.client.cli.repair import run_repair as enrich_fn
    now = time.time() if now is None else now

    libraries = client.get("/v1/libraries").json()
    if only:
        libraries = [lib for lib in libraries if lib["name"] in only]
    skip = set() if _vision_configured(client) else set(VISION_STEPS)

    reachable: dict[str, bool] = {}
    for library in libraries:
        name = library["name"]
        # require_entries: an unmounted mount point is an empty folder, and
        # scanning it would mark every file missing.
        root = reachable_root(library, require_entries=True)
        reachable[library["library_id"]] = root is not None
        if root is None:
            logger.info("worker: %s isn't reachable from here; not scanning it", name)
            continue
        try:
            _scan_library(client, library, root, state, now=now, full_scan_every=full_scan_every,
                          scan_fn=scan_fn, console=console)
        except Exception:  # noqa: BLE001 — one library's trouble doesn't stop the rest
            logger.exception("worker: scanning %s failed; trying again next cycle", name)

    deadline = clock() + enrich_budget

    def out_of_time() -> bool:
        return clock() >= deadline

    # Least recently enriched first, so one library's long backlog doesn't
    # keep the others waiting.
    others_ran = False
    for library in sorted(libraries, key=lambda lib: state.last_enrich.get(lib["library_id"], ((), float("-inf")))[1]):
        if clock() >= deadline - ENRICH_MIN_START_SEC:
            logger.info("worker: out of time for enrichment this cycle; %s and the rest go on next cycle",
                        library["name"])
            break
        try:
            others_ran |= _enrich_library(
                client, library, state, reachable=reachable[library["library_id"]], skip=skip, now=now,
                retry_every=retry_every, enrich_fn=enrich_fn, should_stop=out_of_time, after_others=others_ran,
                console=console)
        except Exception:  # noqa: BLE001
            logger.exception("worker: enriching %s failed; trying again next cycle", library["name"])


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
    should_stop: Callable[[], bool],
    after_others: bool = False,
    console: Console,
) -> bool:
    """Enrich `library` if it has work and isn't paced; True if it ran.

    after_others: other libraries used some of this cycle's time first.
    """
    library_id = library["library_id"]

    def look() -> tuple:
        summary = client.get("/v1/assets/repair-summary", params={"library_id": library_id}).json()
        return _fingerprint(summary, reachable, skip)

    before = look()
    if not any(before[1]):
        return False
    previous = state.last_enrich.get(library_id)
    if previous is not None and previous[0] == before and now - previous[1] < retry_every:
        return False  # nothing changed since the last try
    taken = {item: due for item, due in state.taken.get(library_id, {}).items() if due > now}
    state.taken[library_id] = taken
    waiting = set(taken)
    took = False

    def on_take(step: str, asset_id: str) -> None:
        nonlocal took
        taken[(step, asset_id)] = now + retry_every
        took = True

    try:
        enrich_fn(client, library, job_type="all", console=console, skip_types=skip, should_stop=should_stop,
                  skip_items=waiting, on_take=on_take)
    except (Exception, SystemExit) as exc:
        # Paced like any try, so a crash isn't repeated every cycle.
        state.last_enrich[library_id] = (before, now)
        if isinstance(exc, SystemExit) and exc.code in (0, None):
            raise  # SIGTERM (main.py's handler): the worker is stopping
        logger.exception("worker: enriching %s failed; trying again when it changes or in an hour",
                         library["name"])
        return True
    after = look()
    if not should_stop():
        state.last_enrich[library_id] = (after, now)
    elif took or after != before:
        state.last_enrich[library_id] = (None, now)  # cut short, not stuck
    elif not after_others:
        state.last_enrich[library_id] = (after, now)  # it had the cycle to itself and got nowhere
    # Otherwise time ran out before it took anything, the others having used
    # most of it: it isn't paced, and goes first next cycle.
    return True


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
    if removed := clear_leftovers():
        logger.info("worker: removed %d half-made analysis proxies left by an earlier run", removed)
    # Beside the worker lock.
    state_path = cache_dir("worker-state.json")
    state = load_state(state_path)
    saved = _saved_form(state)
    client = LumiverbClient()
    while True:
        try:
            run_cycle(client, state=state, console=console, full_scan_every=full_scan_every, only=only)
        except Exception:  # noqa: BLE001 — e.g. the API is restarting
            logger.exception("worker: cycle failed; trying again in %.0fs", poll)
        finally:
            # Also when stopped mid-cycle (SIGTERM), so a full scan just done isn't done again.
            if (now_saved := _saved_form(state)) != saved:
                save_state(state_path, now_saved)
                saved = now_saved
        if once or stop():
            return
        sleep(poll)
        if stop():
            return
