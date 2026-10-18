"""Scanning: what changed on storage, and each library in full every 15 minutes (ADR-016 phase 4).

The scheduler looks at each account's libraries every 30 seconds:

1. If a library's storage is reachable from here and the storage machine
   reported changes (or a full scan is due), scan the one folder that
   covers them, then acknowledge the changes the scan saw. A file that
   fails to scan doesn't hold its changes back; it's tried again on its
   own, after 5 minutes, then 10, 20 and so on up to a day. Until then
   other scans, full scans too, leave it alone (found, so never missing,
   but not read), unless its size or modified time changed since it
   failed: replaced, say, it's tried at once. A folder that can't be listed
   (no permission, or the CIFS mount timing out) keeps the changes, which
   wait for the same back-off, and nothing is taken for missing.
2. Each library's storage is looked at again right before its scan (an
   unmounted mount point is an empty folder, and scanning it would mark
   every file missing). What each look finds goes into the account's
   record of its storage (on_roots), which the scheduler otherwise keeps
   itself, every 30 seconds whatever is paused but Pause all
   (account.py look_at_storage): pausing scans stops no other work.

Each library is scanned in full every 15 minutes, unless this machine's
setting says otherwise (LUMIVERB_FULL_SCAN_EVERY_MIN, settings.py), and at
the next pass when a scan of it was asked for (Scan now: the check-in
before each pass says, ADR-018 phase 4): nothing
reports changes on a Mac running the server (no file-system watching), so a
full scan is how its new files are found, and elsewhere it catches a report
that was missed. Each is a plain full scan (Robert, Oct 13): "modified since
the last scan" would miss copies, which keep the camera's time, renames,
which keep theirs, and deletions, which have none.

A library is skipped, never failed: the next look tries again. A library
whose scan itself keeps failing (not one file: the scan raised) is tried
again after a minute, then 2, 4 and so on up to half an hour, and from the
first success as before; its reported changes wait meanwhile, and Settings
→ Processing says so on the Scans row (src/shared/waits.py SCAN_FAILING).
(Moved from the worker, src/client/cli/worker.py, which the scheduler replaces.)
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from rich.console import Console

from src.processing.api import LumiverbAPIError
from src.processing.cache_dir import cache_dir
from src.processing.machine import FULL_SCAN_EVERY_MIN
from src.processing.originals import Originals, reach
from src.shared.io_utils import UnsafeRelPathError, is_within

if TYPE_CHECKING:
    from src.processing.scan import ScanStats

logger = logging.getLogger(__name__)

# Unless the scheduler's setting says otherwise (runners.scan passes it).
DEFAULT_FULL_SCAN_EVERY_SEC = FULL_SCAN_EVERY_MIN * 60
# Files that failed to scan are tried again after this, doubling up to a day.
RETRY_FIRST_SEC = 300.0
RETRY_MAX_SEC = 24 * 3600.0
# A library whose scan failed is tried again after this, doubling up to half an hour.
FAIL_FIRST_SEC = 60.0
FAIL_MAX_SEC = 30 * 60.0
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
    # Each failed file's size and modified time when it failed: still so, it
    # waits for this retry whatever scan comes first (a full scan every 15
    # minutes would otherwise try a broken file 96 times a day).
    seen: dict[str, tuple[int, str | None]] = field(default_factory=dict)


@dataclass
class Failing:
    """A library whose scans keep failing: the latest error, since when (wall
    time, as now), and when it's tried again."""

    error: str
    since: float
    delay: float
    due: float


@dataclass
class ScanState:
    """What scanning remembers between passes. Full-scan times and retries
    outlive the process (save_state), so a restart doesn't rescan everything,
    nor forget what's waiting to be tried again. Libraries whose scans keep
    failing don't: a new start (an upgrade with the fix, often) tries each at once."""

    last_full_scan: dict[str, float] = field(default_factory=dict)
    retries: dict[str, Retry] = field(default_factory=dict)
    failing: dict[str, Failing] = field(default_factory=dict)


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
                            {str(c): int(v) for c, v in r["held"].items()},
                            {str(p): (int(size), None if mtime is None else str(mtime))
                             for p, (size, mtime) in r.get("seen", {}).items()})
            for lib, r in saved.get("retries", {}).items()
        }
    return state


def saved_form(state: ScanState) -> dict:
    # Copied first (one step under the GIL): a scan job changes them meanwhile.
    retries = dict(state.retries)
    return {
        "last_full_scan": dict(state.last_full_scan),
        "retries": {lib: {"paths": sorted(r.paths), "delay": r.delay, "due": r.due, "held": dict(r.held),
                          "seen": {p: list(s) for p, s in r.seen.items()}}
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


def _is_dir(originals: Originals, rel: str) -> bool:
    """False when it can't be checked: its parent's scan covers it either way."""
    try:
        st = originals.stat(rel)
    except (OSError, UnsafeRelPathError):  # a path that leaves the library is no folder of it
        return False
    return st is not None and st.folder


def scan_library(
    client: Any,
    library: dict,
    originals: Originals,
    state: ScanState,
    *,
    now: float,
    full_scan_every: float,
    scan_fn: Callable,
    console: Console,
    asked: bool = False,
    cache_previews: bool | None = None,
    to_upload: Callable[[int, int], None] | None = None,
    remakes: bool = True,
) -> None:
    """Scan what's due in the library: the folder covering its reported
    changes and retries, or all of it when a full scan is due or one was
    asked for (asked: Scan now). cache_previews: run_scan's; None, the
    scheduler's way (on a machine apart from the API). to_upload, remakes:
    run_scan's."""
    library_id = library["library_id"]
    resp = client.get(f"/v1/libraries/{library_id}/changes").json()
    changes: list[dict] = resp.get("changes", [])
    retry = state.retries.get(library_id)
    retry_due = retry is not None and now >= retry.due
    if retry is not None and not retry_due:
        changes = [c for c in changes if retry.held.get(c["change_id"]) != c["version"]]
    full_due = asked or now - state.last_full_scan.get(library_id, float("-inf")) >= full_scan_every
    if not changes and not retry_due and not full_due:
        return

    if full_due or resp.get("truncated"):
        prefix = None
    else:
        paths = [c["rel_path"] for c in changes] + (sorted(retry.paths) if retry_due else [])
        prefix = scan_scope(paths, lambda rel: _is_dir(originals, rel))
    logger.info(
        "scheduler: scanning %s%s (%d reported change(s)%s%s)",
        library["name"], f" / {prefix}" if prefix else "", len(changes),
        ", retrying what failed" if retry_due else "",
        ", a scan asked for" if asked else ", full scan due" if full_due else "",
    )
    # Files waiting for a retry that isn't due are left to it, unless they changed.
    waiting = {} if retry is None or retry_due else {p: s for p, s in retry.seen.items() if p in retry.paths}
    # When most of a library looks gone the server asks first (409
    # mass_missing, ADR-016): a person answers, never the scheduler.
    if cache_previews is None:
        from src.server.scheduler.service import stores_here

        cache_previews = not stores_here()
    stats = scan_fn(client, library, path_prefix=prefix, allow_moves=True, allow_mass_delete=False,
                    waiting=waiting, cache_previews=cache_previews, console=console,
                    **({"to_upload": to_upload} if to_upload is not None else {}),
                    **({} if remakes else {"remakes": False}))
    if stats.root_unreachable:
        logger.warning("scheduler: %s became unreachable during the scan; changes kept", library["name"])
        return
    if stats.mass_missing:
        logger.warning("scheduler: in %s, %d clip(s) look missing, most of the library; not archived. "
                       "If they're really gone: lumiverb scan --library %s --allow-mass-delete",
                       library["name"], stats.mass_missing, shlex.quote(library["name"]))
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
        # Safe to send twice: a change acknowledged is gone, and one reported again since stays.
        client.post(
            f"/v1/libraries/{library_id}/changes/ack",
            json={"changes": [{"change_id": c["change_id"], "version": c["version"]} for c in seen]},
            retry=True,
        )


def _note_failures(
    state: ScanState, library: dict, prefix: str | None, stats: ScanStats, changes: list[dict], now: float,
) -> None:
    """Remember files that failed to scan (with their size and modified time
    then) and folders that couldn't be listed, to try them again with
    back-off. A folder that couldn't be listed also holds back the changes
    the scan covered.

    A scan covering a path clears it unless it fails again, or the scan left
    it for its retry (unchanged since it failed).
    """
    library_id = library["library_id"]
    failed = set(stats.failed_paths) | set(stats.unlisted)
    if stats.failed and not stats.failed_paths:
        failed.add(prefix or "")
    old = state.retries.pop(library_id, None)
    scanned = {c["change_id"] for c in changes}
    waiting = set(stats.waiting_paths)
    left = {p for p in old.paths if not is_within(p, prefix) or p in waiting} if old else set()
    held = {cid: v for cid, v in old.held.items() if cid not in scanned} if old else {}
    if stats.unlisted:
        held |= {c["change_id"]: c["version"] for c in changes}
    seen = {**(old.seen if old else {}), **stats.failed_seen}  # as each last failed
    if failed:
        delay = min(old.delay * 2, RETRY_MAX_SEC) if old else RETRY_FIRST_SEC
        paths = left | failed
        state.retries[library_id] = Retry(paths, delay, now + delay, held,
                                          {p: s for p, s in seen.items() if p in paths})
        logger.warning("scheduler: in %s, %d file(s) or folder(s) failed to scan; trying again in %.0f min",
                       library["name"], len(failed), delay / 60)
    elif old and (left or held):
        state.retries[library_id] = Retry(left, old.delay, old.due, held,
                                          {p: s for p, s in seen.items() if p in left})


# Whether this process has said its root map is empty (once per start).
_said_no_root_map = False


def _warn_if_no_root_map() -> None:
    """A library out of reach with no root map: the likely cause, said once."""
    global _said_no_root_map
    from src.processing.machine import current

    if _said_no_root_map or current().root_map:
        return
    _said_no_root_map = True
    logger.warning("scheduler: LUMIVERB_ROOT_MAP is empty; no library can be scanned")


def scan_pass(
    client: Any,
    libraries: list[dict],
    state: ScanState,
    *,
    now: float,
    full_scan_every: float = DEFAULT_FULL_SCAN_EVERY_SEC,
    scan_fn: Callable | None = None,
    console: Console | None = None,
    on_roots: Callable[[dict[str, Originals | None]], None] | None = None,
    scan_now: set[str] | frozenset[str] = frozenset(),
    scanning: Callable[[str | None], None] | None = None,
    cache_previews: bool | None = None,
    to_upload: Callable[[str, int, int], None] | None = None,
    remakes: bool = True,
) -> dict[str, Originals | None]:
    """Scan what's due in each library whose storage is reachable; each
    library's originals on this machine (src/processing/originals.py; None:
    not reachable). on_roots hears what every library's look found before
    any scan, then a library's alone whenever it changed. Each library is
    looked at again right before its scan: a share can sleep during another
    library's long scan. scan_now: libraries a scan of which was asked for,
    scanned whole now; scanning hears each library as its scan starts, and
    None when it ends. cache_previews: scan_library's (a device keeps none).
    to_upload hears, by library, the files its scan has found to add or read
    again and hasn't sent yet, and their size on disk (run_scan's).
    remakes: run_scan's (False: the reader holds its remakes back)."""
    if scan_fn is None:
        from src.processing.scan import run_scan as scan_fn
    console = console or Console(quiet=True)
    # require_entries: an unmounted mount point is an empty folder, and
    # scanning it would mark every file missing.
    found: dict[str, Originals | None] = {
        library["library_id"]: reach(library, require_entries=True) for library in libraries}
    if on_roots is not None:
        on_roots(dict(found))
    for i, library in enumerate(libraries):
        originals = found[library["library_id"]]
        if i > 0 and originals is not None:  # the first was looked at just now
            originals = reach(library, require_entries=True)
            if originals != found[library["library_id"]]:
                found[library["library_id"]] = originals
                if on_roots is not None:  # the others' looks may be older than the scheduler's since
                    on_roots({library["library_id"]: originals})
        if originals is None:
            logger.info("scheduler: %s isn't reachable from here; not scanning it", library["name"])
            _warn_if_no_root_map()
            continue
        failing = state.failing.get(library["library_id"])
        if failing is not None and now < failing.due:
            continue  # its back-off: the scheduler's status says why
        if scanning is not None:
            scanning(library["library_id"])
        try:
            scan_library(client, library, originals, state, now=now, full_scan_every=full_scan_every,
                         scan_fn=scan_fn, console=console, asked=library["library_id"] in scan_now,
                         cache_previews=cache_previews,
                         to_upload=partial(to_upload, library["library_id"]) if to_upload is not None else None,
                         remakes=remakes)
        except LumiverbAPIError as exc:
            if exc.code != "scans_paused":
                _scan_failed(state, library, exc, now)
            else:  # paused since the libraries were listed: its changes wait, as a paused library's do
                logger.info("scheduler: %s's scans were paused; not scanning it", library["name"])
        except Exception as exc:  # noqa: BLE001 — one library's trouble doesn't stop the rest
            _scan_failed(state, library, exc, now)
        else:
            if state.failing.pop(library["library_id"], None) is not None:
                logger.info("scheduler: scanning %s works again", library["name"])
        finally:
            if scanning is not None:
                scanning(None)
    return found


def _scan_failed(state: ScanState, library: dict, exc: Exception, now: float) -> None:
    """A library's scan raised: it's tried again after FAIL_FIRST_SEC, twice
    as long each time it fails again, up to FAIL_MAX_SEC."""
    library_id = library["library_id"]
    error = (str(exc).strip().splitlines() or [type(exc).__name__])[0][:300]
    old = state.failing.get(library_id)
    delay = min(old.delay * 2, FAIL_MAX_SEC) if old else FAIL_FIRST_SEC
    state.failing[library_id] = Failing(error, old.since if old else now, delay, now + delay)
    if old is None:
        logger.exception("scheduler: scanning %s failed; trying again in %.0f min", library["name"], delay / 60)
    else:
        logger.warning("scheduler: scanning %s failed again (%s); trying again in %.0f min",
                       library["name"], error, delay / 60)


def failing_now(state: ScanState | None, library_ids: list[str]) -> list[dict]:
    """Of these libraries, those whose scans keep failing: the error, since
    when and the next try (ISO times), as the scheduler's status says them."""
    from datetime import UTC, datetime

    def iso(when: float) -> str:
        return datetime.fromtimestamp(when, UTC).isoformat()

    failing = dict(state.failing) if state is not None else {}  # a copy: a scan job changes it meanwhile
    return [{"library_id": i, "error": f.error, "since": iso(f.since), "next_try": iso(f.due)}
            for i in library_ids if (f := failing.get(i)) is not None]
