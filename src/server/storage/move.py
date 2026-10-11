"""Moving the previews to another folder (Settings › Storage): off a Mac's
system drive, say. Only the previews (every storage key: the account
folders in the previews folder) move; the database, the search index,
models and caches stay in DATA_DIR.

start() records the new folder and where they're moving from
(src/shared/previews_location.py), so new files go to the new folder at
once and the rest are read where they are (src/server/storage/local.py);
then moves them in a thread of its own. Lumiverb keeps working meanwhile.

Each file under the old folder's account folders: one already in the new
folder (written there since the move began) is newer, and the old copy
goes; otherwise it's renamed across (the same disk) or copied beside its
new place and linked in, never over a file written meanwhile, then the old
one removed. Playback cuts aren't moved: they're made again on demand.

It stops at the first error (the new drive full or unplugged, the old one
gone) and says why; the 5-minute upkeep starts it again, as does Try
again. A move the API was restarted in the middle of goes on the same way.
Once every file is across, the old account folders (empty by then) are
removed and storage.json forgets where from.

One move at a time per install (a lock in DATA_DIR); a new one waits for
the last to finish.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.shared import previews_location

logger = logging.getLogger(__name__)

STATE_FILE = "storage-move.json"
LOCK_FILE = "storage-move.lock"
ACCOUNT_PREFIX = "ten_"
PLAYBACK = "playback"
MOVING_SUFFIX = ".moving"
# Room to spare on the new disk beyond what's moved.
SPARE_FRACTION = 0.05
SPARE_BYTES = 1024**3
# Progress written this often (files), so every API worker can show it.
PROGRESS_EVERY = 200
# An upload's temp file this old was left by a request that died.
STALE_TMP_SEC = 3600
# A move that stopped on an error is tried again (by upkeep) this much later.
RETRY_AFTER_SEC = 15 * 60


class MoveError(Exception):
    """A move that can't start, or stopped; the message says why, plainly."""


@dataclass(frozen=True)
class Size:
    files: int
    bytes: int


# -- What's there ----------------------------------------------------------------


def _accounts(root: Path) -> list[Path]:
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(ACCOUNT_PREFIX))
    except OSError:
        return []


def _files(root: Path) -> Iterator[Path]:
    """Every file in the root's account folders, playback cuts too."""
    for account in _accounts(root):
        for folder, _dirs, names in os.walk(account):
            for name in names:
                yield Path(folder, name)


def size_of(root: Path) -> Size:
    """How much the previews in `root` are: what a move carries."""
    files = total = 0
    for path in _files(root):
        if path.relative_to(root).parts[1:2] == (PLAYBACK,):
            continue
        try:
            total += path.stat().st_size
            files += 1
        except OSError:
            pass
    return Size(files, total)


def check_target(data_dir: Path, to: str | Path, need: Size | None = None) -> Path:
    """The folder, when the previews can move there: a full path to a folder
    this machine's Lumiverb can write, not where they are, not inside them,
    and with room for them (`need`). Raises MoveError saying why not."""
    raw = str(to).strip()
    if not raw:
        raise MoveError("Say which folder.")
    chosen = Path(raw).expanduser()
    if not chosen.is_absolute():
        raise MoveError("Use a full path, such as /Volumes/Media/Lumiverb previews.")
    chosen = Path(os.path.abspath(chosen))
    if not chosen.is_dir():
        raise MoveError(f"There's no folder at {chosen}.")
    current = previews_location.read(data_dir).previews
    if chosen == current:
        raise MoveError(f"The previews are in {chosen} already.")
    for root in (current, Path(os.path.abspath(data_dir))):
        with contextlib.suppress(ValueError):
            if chosen.relative_to(root).parts[:1] and chosen.relative_to(root).parts[0].startswith(ACCOUNT_PREFIX):
                raise MoveError("That folder is inside the previews themselves.")
    try:
        probe = chosen / f".lumiverb-write-check-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        raise MoveError(f"Lumiverb can't write to {chosen}.") from None
    if need is not None and not same_disk(chosen, current):
        free = shutil.disk_usage(chosen).free
        wanted = int(need.bytes * (1 + SPARE_FRACTION)) + SPARE_BYTES
        if free < wanted:
            raise MoveError(f"There isn't room: the previews are {size_text(need.bytes)} and {chosen} has "
                            f"{size_text(free)} free.")
    return chosen


def same_disk(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def size_text(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# -- State --------------------------------------------------------------------------


def read_state(data_dir: Path) -> dict:
    try:
        state = json.loads((data_dir / STATE_FILE).read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _update_state(data_dir: Path, **changes) -> None:
    path = data_dir / STATE_FILE
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(json.dumps({**read_state(data_dir), **changes}, indent=2))
    partial.replace(path)


@contextlib.contextmanager
def _one_at_a_time(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / LOCK_FILE, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MoveError("The previews are being moved already.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def running(data_dir: Path) -> bool:
    try:
        with _one_at_a_time(data_dir):
            return False
    except MoveError:
        return True
    except OSError:
        return False


# -- Moving ---------------------------------------------------------------------------


def start(data_dir: Path, to: str | Path, *, background: bool = True) -> Path:
    """Moves the previews to `to`: new files go there from now on, and the
    rest move in the background. Returns the folder. Raises MoveError
    before changing anything when it can't."""
    data_dir = Path(os.path.abspath(data_dir))
    location = previews_location.read(data_dir)
    if location.moving_from is not None:
        raise MoveError("The previews are being moved already: wait for that to finish.")
    need = size_of(location.previews)
    chosen = check_target(data_dir, to, need)
    with _one_at_a_time(data_dir):
        if chosen != data_dir:
            (chosen / previews_location.MARKER).write_text(
                "Lumiverb's previews. Settings › Storage moves them; don't delete this folder.\n")
        previews_location.write(data_dir, chosen, location.previews)
        _update_state(data_dir, started_at=datetime.now(UTC).isoformat(), finished_at=None,
                      from_folder=str(location.previews), to_folder=str(chosen),
                      total_files=need.files, total_bytes=need.bytes, done_files=0, done_bytes=0,
                      error=None, failed_at=None)
    logger.info("Moving the previews (%d files, %d bytes) from %s to %s", need.files, need.bytes,
                location.previews, chosen)
    if background:
        go_on(data_dir)
    else:
        run(data_dir)
    return chosen


def go_on(data_dir: Path, *, now: float | None = None, waits: bool = False) -> str:
    """A move under way and not running (stopped by an error, or the API
    restarted): goes on in the background. With `waits` (upkeep), one that
    stopped on an error waits 15 minutes first. "started", "running",
    "waiting" or "not moving"."""
    if previews_location.read(data_dir).moving_from is None:
        return "not moving"
    if running(data_dir):
        return "running"
    failed = read_state(data_dir).get("failed_at")
    if waits and isinstance(failed, str):
        with contextlib.suppress(ValueError):
            since = (now or time.time()) - datetime.fromisoformat(failed).timestamp()
            if since < RETRY_AFTER_SEC:
                return "waiting"

    def work() -> None:
        with contextlib.suppress(MoveError):
            run(data_dir)

    threading.Thread(target=work, name="previews-move", daemon=True).start()
    return "started"


def run(data_dir: Path) -> None:
    """Moves what's left, here and now. Raises MoveError (recorded) when it stops."""
    data_dir = Path(os.path.abspath(data_dir))
    with _one_at_a_time(data_dir):
        location = previews_location.read(data_dir)
        old, new = location.moving_from, location.previews
        if old is None:
            return
        _update_state(data_dir, error=None, failed_at=None)
        try:
            _both_there(data_dir, location)
            _move_all(data_dir, location)
        except MoveError as exc:
            _stopped(data_dir, str(exc))
            raise
        except OSError as exc:
            said = _said(exc)
            _stopped(data_dir, said)
            raise MoveError(said) from exc
        _remove_empty(old)
        previews_location.write(data_dir, new, None)
        _update_state(data_dir, finished_at=datetime.now(UTC).isoformat(), error=None, failed_at=None)
        logger.info("The previews are in %s now", new)


def _stopped(data_dir: Path, said: str) -> None:
    _update_state(data_dir, error=said, failed_at=datetime.now(UTC).isoformat())
    logger.error("Moving the previews stopped: %s", said)


def _said(exc: OSError) -> str:
    where = f" ({exc.filename})" if exc.filename else ""
    if exc.errno == errno.ENOSPC:
        return f"The new drive is full{where}."
    if exc.errno in (errno.ENOENT, errno.EIO, errno.ENXIO, errno.ENODEV):
        return f"A drive went away{where}: {exc.strerror}. Is it plugged in?"
    return f"{exc.strerror or exc}{where}"


def _both_there(data_dir: Path, location: previews_location.Location) -> None:
    """Both folders' drives are plugged in. Checked as it goes: a file not
    found on a drive that went away isn't a file deleted meanwhile."""
    if not location.available(data_dir):
        raise MoveError(f"The new folder isn't there: {location.previews}. Is its drive plugged in?")
    if location.moving_from is not None and not location.available(data_dir, location.moving_from):
        raise MoveError(f"The old folder isn't there: {location.moving_from}. Is its drive plugged in?")


def _move_all(data_dir: Path, location: previews_location.Location) -> None:
    old, new = location.moving_from, location.previews
    state = read_state(data_dir)
    done_files, done_bytes = int(state.get("done_files") or 0), int(state.get("done_bytes") or 0)
    since = 0
    for path in _files(old):
        rel = path.relative_to(old)
        if rel.parts[1:2] == (PLAYBACK,):
            path.unlink(missing_ok=True)  # made again on demand
            continue
        if path.name.endswith((".tmp", MOVING_SUFFIX)):
            # An upload's temp file, or a copy of ours a stop left: in use while young.
            with contextlib.suppress(OSError):
                if time.time() - path.stat().st_mtime > STALE_TMP_SEC:
                    path.unlink()
            continue
        try:
            size = path.stat().st_size
            _move_one(path, new / rel)
        except FileNotFoundError:
            _both_there(data_dir, location)
            if path.exists():
                raise
            continue  # deleted meanwhile
        done_files += 1
        done_bytes += size
        since += 1
        if since >= PROGRESS_EVERY:
            _both_there(data_dir, location)
            _update_state(data_dir, done_files=done_files, done_bytes=done_bytes)
            since = 0
    _both_there(data_dir, location)  # nothing listed because a drive went away isn't done
    _update_state(data_dir, done_files=done_files, done_bytes=done_bytes)


def _move_one(src: Path, dest: Path) -> None:
    """src to dest, never over a file written there meanwhile (that one is
    newer: src just goes)."""
    if dest.exists():
        src.unlink(missing_ok=True)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)  # the same disk: no copy, and never over another
        src.unlink()
        return
    except FileExistsError:
        src.unlink(missing_ok=True)
        return
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK):
            raise
    partial = dest.with_name(dest.name + MOVING_SUFFIX)
    shutil.copy2(src, partial)
    if partial.stat().st_size != src.stat().st_size:
        partial.unlink(missing_ok=True)
        raise MoveError(f"{dest} didn't copy whole.")
    try:
        os.link(partial, dest)
        partial.unlink()
    except FileExistsError:
        partial.unlink(missing_ok=True)  # written meanwhile: newer
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK):
            raise
        # A disk without links (exFAT): as close to never-over as it allows.
        if dest.exists():
            partial.unlink(missing_ok=True)
        else:
            partial.rename(dest)
    src.unlink(missing_ok=True)


def _remove_empty(old: Path) -> None:
    """The old account folders, once nothing's left in them."""
    for account in _accounts(old):
        for folder, _dirs, _names in sorted(os.walk(account), key=lambda w: len(w[0]), reverse=True):
            with contextlib.suppress(OSError):
                Path(folder).rmdir()
