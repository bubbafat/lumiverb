"""Scanning: what changed on storage, and each library in full once a day (ADR-016 phase 4).

The scheduler looks at each account's libraries every 30 seconds:

1. If a library's storage is reachable from here and the storage machine
   reported changes (or a full scan is due), scan the one folder that
   covers them, then acknowledge the changes the scan saw. A file that
   fails to scan doesn't hold its changes back; it's tried again on its
   own, after 5 minutes, then 10, 20 and so on up to a day. A folder that
   can't be listed (no permission, or the CIFS mount timing out) keeps the
   changes, which wait for the same back-off, and nothing is taken for
   missing.
2. Which libraries' storage is reachable is what probing and rendering
   (which read the originals) go by until the next look.

A library is skipped, never failed: the next look tries again.
(Moved from the worker, src/client/cli/worker.py, which the scheduler replaces.)
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from stat import S_ISDIR
from typing import TYPE_CHECKING, Any

from rich.console import Console

from src.client.cache_dir import cache_dir
from src.client.cli.roots import reachable_root
from src.shared.io_utils import is_within, resolve_source_path, stat_if_present

if TYPE_CHECKING:
    from src.client.cli.scan import ScanStats

logger = logging.getLogger(__name__)

DEFAULT_FULL_SCAN_EVERY_SEC = 24 * 3600.0
# Files that failed to scan are tried again after this, doubling up to a day.
RETRY_FIRST_SEC = 300.0
RETRY_MAX_SEC = 24 * 3600.0
# Where the full-scan times and retries are kept (the worker's file, so they
# carry over from it).
STATE_FILE = "worker-state.json"


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
class ScanState:
    """What scanning remembers between passes. Full-scan times and retries
    outlive the process (save_state), so a restart doesn't rescan everything,
    nor forget what's waiting to be tried again."""

    last_full_scan: dict[str, float] = field(default_factory=dict)
    retries: dict[str, Retry] = field(default_factory=dict)


def load_state(path: Path) -> ScanState:
    """Full-scan times and retries as an earlier run saved them; each empty if unreadable."""
    state = ScanState()
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


def saved_form(state: ScanState) -> dict:
    # Copied first (one step under the GIL): a scan job changes them meanwhile.
    retries = dict(state.retries)
    return {
        "last_full_scan": dict(state.last_full_scan),
        "retries": {lib: {"paths": sorted(r.paths), "delay": r.delay, "due": r.due, "held": dict(r.held)}
                    for lib, r in retries.items()},
    }


def save_state(path: Path, saved: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        part.write_text(json.dumps(saved))
        part.replace(path)
    except OSError as exc:
        logger.warning("scheduler: couldn't save its state to %s: %s", path, exc)


class ServiceLock:
    """One scheduler per machine: two would scan and render the same files.
    It takes the old worker's lock file, so a worker left running and the
    scheduler never both run."""

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


def _is_dir(root: Path, rel: str) -> bool:
    """False when it can't be checked: its parent's scan covers it either way."""
    try:
        st = stat_if_present(resolve_source_path(root, rel))
    except OSError:
        return False
    return st is not None and S_ISDIR(st.st_mode)


def scan_library(
    client: Any,
    library: dict,
    root: Path,
    state: ScanState,
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
        "scheduler: scanning %s%s (%d reported change(s)%s%s)",
        library["name"], f" / {prefix}" if prefix else "", len(changes),
        ", retrying what failed" if retry_due else "", ", full scan due" if full_due else "",
    )
    # Archiving is reversible (a file that comes back, anywhere in the library,
    # restores its asset), so no mass-delete guard: the mount check and
    # unreadable folders are what keep a glitch from archiving anything.
    stats = scan_fn(client, library, path_prefix=prefix, allow_moves=True, allow_mass_delete=True, console=console)
    if stats.root_unreachable:
        logger.warning("scheduler: %s became unreachable during the scan; changes kept", library["name"])
        return
    if prefix is None:
        state.last_full_scan[library_id] = now
    _note_failures(state, library, prefix, stats, changes, now)
    if stats.unlisted:
        logger.warning("scheduler: in %s, %d folder(s) couldn't be listed (%s); changes kept for the retry",
                       library["name"], len(stats.unlisted), ", ".join(stats.unlisted[:5]) or "/")
        return

    # A file still being written is scanned next cycle: changes that cover
    # it wait, the rest are done.
    seen = [c for c in changes if not any(is_within(s, c["rel_path"]) for s in stats.settling_paths)]
    if len(seen) < len(changes):
        logger.info("scheduler: in %s, %d file(s) still being written; %d change(s) wait for the next cycle",
                    library["name"], stats.settling, len(changes) - len(seen))
    if seen:
        client.post(
            f"/v1/libraries/{library_id}/changes/ack",
            json={"changes": [{"change_id": c["change_id"], "version": c["version"]} for c in seen]},
        )


def _note_failures(
    state: ScanState, library: dict, prefix: str | None, stats: ScanStats, changes: list[dict], now: float,
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
        logger.warning("scheduler: in %s, %d file(s) or folder(s) failed to scan; trying again in %.0f min",
                       library["name"], len(failed), delay / 60)
    elif old and (left or held):
        state.retries[library_id] = Retry(left, old.delay, old.due, held)


def scan_pass(
    client: Any,
    libraries: list[dict],
    state: ScanState,
    *,
    now: float,
    full_scan_every: float = DEFAULT_FULL_SCAN_EVERY_SEC,
    scan_fn: Callable | None = None,
    console: Console | None = None,
    on_roots: Callable[[dict[str, Path | None]], None] | None = None,
) -> dict[str, Path | None]:
    """Scan what's due in each library whose storage is reachable; each
    library's storage on this machine (None: not reachable). on_roots hears
    where each one's storage is before any scan, so work on the originals
    needn't wait for a long one, and again whenever a library's changes.
    Each library is looked at again right before its scan: a share can
    sleep during another library's long scan."""
    if scan_fn is None:
        from src.client.cli.scan import run_scan as scan_fn
    console = console or Console(quiet=True)
    # require_entries: an unmounted mount point is an empty folder, and
    # scanning it would mark every file missing.
    roots: dict[str, Path | None] = {
        library["library_id"]: reachable_root(library, require_entries=True) for library in libraries}
    if on_roots is not None:
        on_roots(dict(roots))
    for i, library in enumerate(libraries):
        root = roots[library["library_id"]]
        if i > 0 and root is not None:  # the first was looked at just now
            root = reachable_root(library, require_entries=True)
            if root != roots[library["library_id"]]:
                roots[library["library_id"]] = root
                if on_roots is not None:
                    on_roots(dict(roots))
        if root is None:
            logger.info("scheduler: %s isn't reachable from here; not scanning it", library["name"])
            continue
        try:
            scan_library(client, library, root, state, now=now, full_scan_every=full_scan_every,
                         scan_fn=scan_fn, console=console)
        except Exception:  # noqa: BLE001 — one library's trouble doesn't stop the rest
            logger.exception("scheduler: scanning %s failed; trying again at the next look", library["name"])
    return roots
