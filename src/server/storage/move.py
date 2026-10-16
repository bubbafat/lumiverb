"""Moving the previews to another folder: the server's (Settings › Storage;
off a Mac's system drive, say) or one library's (its settings; a big video
library on a drive of its own). Only the previews (storage keys) move; the
database, the search index, models and caches stay in DATA_DIR.

start() records the new folder and where they're moving from
(src/shared/previews_location.py), so new files go to the new folder at
once and the rest are read where they are (src/server/storage/local.py);
then moves them in a thread of its own. Lumiverb keeps working meanwhile.

What a move carries: the server's, every account folder's files but the
libraries with a folder of their own; a library's, its folder in each
account folder. Each file: one already in the new folder (written there
since the move began) is newer, and the old copy goes; otherwise it's
renamed across (the same disk) or copied beside its new place and linked
in, never over a file written meanwhile, then the old one removed.
Playback cuts aren't moved: they're made again on demand.

It stops at the first error (the new drive full or unplugged, the old one
gone) and says why; the 5-minute upkeep starts it again, as does Try
again. A move the API was restarted in the middle of goes on the same way.
Once every file is across, the folders it emptied are removed and
storage.json forgets where from.

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

from src.server import drives, write_check
from src.shared import previews_location
from src.shared.previews_location import Location

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
# running() looks at the lock by taking it for an instant (Settings › Storage
# asks every 2 s). A mover starting in that instant waits this long for it
# before taking the holder for another move: a look lets go at once, a move
# holds it to the end.
LOOK_WAIT_SEC = 2.0


class MoveError(Exception):
    """A move that can't start, or stopped; the message says why, plainly."""


@dataclass(frozen=True)
class Size:
    files: int
    bytes: int


@dataclass(frozen=True)
class Scope:
    """What a move carries: the server's previews (library_id None), or one library's."""

    library_id: str | None = None
    # The server's move leaves these alone: they have folders of their own.
    leaves: frozenset[str] = frozenset()

    def takes(self, rel: Path) -> bool:
        """Whether a file (relative to the old folder: account/library/...) is the move's."""
        library = rel.parts[1] if len(rel.parts) > 1 else None
        if self.library_id is not None:
            return library == self.library_id
        return library not in self.leaves


# -- What's there ----------------------------------------------------------------


def _accounts(root: Path) -> list[Path]:
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and p.name.startswith(ACCOUNT_PREFIX))
    except OSError:
        return []


def _files(root: Path, scope: Scope) -> Iterator[Path]:
    """The scope's files in the root's account folders, playback cuts too."""
    for account in _accounts(root):
        tops = [account / scope.library_id] if scope.library_id else [account]
        for top in tops:
            for folder, dirs, names in os.walk(top):
                if scope.library_id is None and Path(folder) == account:
                    dirs[:] = [d for d in dirs if d not in scope.leaves]
                for name in names:
                    yield Path(folder, name)


def size_of(root: Path, scope: Scope | None = None) -> Size:
    """How much the scope's previews in `root` are: what a move carries."""
    scope = scope or Scope()
    files = total = 0
    for path in _files(root, scope):
        if path.relative_to(root).parts[1:2] == (PLAYBACK,):
            continue
        try:
            total += path.stat().st_size
            files += 1
        except OSError:
            pass
    return Size(files, total)


def check_target(data_dir: Path, to: str | Path, need: Size | None = None, *, current: Path | None = None) -> Path:
    """The folder, when previews can move there: a full path to a folder this
    machine's Lumiverb can write, not where they are (`current`: the server's
    previews folder unless said), not inside any previews folder's accounts,
    and with room for them (`need`). Raises MoveError saying why not."""
    raw = str(to).strip()
    if not raw:
        raise MoveError("Say which folder.")
    chosen = Path(raw).expanduser()
    if not chosen.is_absolute():
        raise MoveError("Use a full path, such as /Volumes/Media/Lumiverb previews.")
    chosen = Path(os.path.abspath(chosen))
    location = previews_location.read(data_dir)
    current = current or location.previews
    if chosen == current:
        raise MoveError(f"The previews are in {chosen} already.")
    if (why := drives.missing(chosen)) is not None:
        raise MoveError(why)
    if not chosen.is_dir():
        raise MoveError(f"There's no folder at {chosen}.")
    for root in (*location.roots, Path(os.path.abspath(data_dir))):
        with contextlib.suppress(ValueError):
            first = chosen.relative_to(root).parts[:1]
            if first and first[0].startswith(ACCOUNT_PREFIX):
                raise MoveError("That folder is inside the previews themselves.")
    if (why := write_check.try_write(chosen)) is not None:
        raise MoveError(why)
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
def _one_at_a_time(data_dir: Path, *, wait: float = 0.0) -> Iterator[None]:
    """Held while a move starts or runs; `wait`: how long to wait out a look."""
    data_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    with open(data_dir / LOCK_FILE, "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MoveError("The previews are being moved already.") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def running(data_dir: Path) -> bool:
    """Whether a move holds the lock now: a look, taking it for an instant."""
    try:
        with _one_at_a_time(data_dir):
            return False
    except MoveError:
        return True
    except OSError:
        return False


# -- Starting ---------------------------------------------------------------------------


def plan(data_dir: Path, to: str | Path | None, *, library_id: str | None = None) -> tuple[Path, Path, Size]:
    """(from, to, what moves) for moving the server's previews (library_id
    None; `to` None: back to DATA_DIR) or a library's (`to` None: back to
    the server's folder). Raises MoveError when it can't."""
    data_dir = Path(os.path.abspath(data_dir))
    location = previews_location.read(data_dir)
    if location.moving:
        raise MoveError("The previews are being moved already: wait for that to finish.")
    if library_id is None:
        old, target = location.previews, (data_dir if to is None else to)
        scope = Scope(leaves=frozenset(location.libraries))
    else:
        old, target = location.root_for(library_id), (location.previews if to is None else to)
        scope = Scope(library_id)
    need = size_of(old, scope)
    return old, check_target(data_dir, target, need, current=old), need


def start(data_dir: Path, to: str | Path | None, *, library_id: str | None = None, background: bool = True) -> Path:
    """Moves the server's previews (or a library's) to `to`: new files go
    there from now on, and the rest move in the background. Returns the
    folder. Raises MoveError before changing anything when it can't."""
    data_dir = Path(os.path.abspath(data_dir))
    old, chosen, need = plan(data_dir, to, library_id=library_id)
    with _one_at_a_time(data_dir, wait=LOOK_WAIT_SEC):
        location = previews_location.read(data_dir)
        if chosen != data_dir:
            (chosen / previews_location.MARKER).write_text(
                "Lumiverb's previews. Settings › Storage moves them; don't delete this folder.\n")
        if library_id is None:
            previews_location.write(data_dir, chosen, old)
        else:
            libraries = dict(location.libraries)
            if chosen == location.previews:
                libraries.pop(library_id, None)  # back to the server's folder
            else:
                libraries[library_id] = chosen
            previews_location.write(data_dir, location.previews, location.moving_from, libraries=libraries,
                                    libraries_moving_from={**location.libraries_moving_from, library_id: old})
        _update_state(data_dir, started_at=datetime.now(UTC).isoformat(), finished_at=None,
                      library_id=library_id, from_folder=str(old), to_folder=str(chosen),
                      total_files=need.files, total_bytes=need.bytes, done_files=0, done_bytes=0,
                      error=None, failed_at=None)
    logger.info("Moving %s previews (%d files, %d bytes) from %s to %s",
                f"library {library_id}'s" if library_id else "the", need.files, need.bytes, old, chosen)
    if background:
        go_on(data_dir)
    else:
        run(data_dir)
    return chosen


def settled(data_dir: Path, *, within: float = 1.0) -> bool:
    """Whether no move is under way, waiting up to `within` seconds for one
    to finish: a small one (or one with nothing to carry) is done in a
    moment, so the answer to starting it shows it done; a big one shows
    how far it got."""
    deadline = time.monotonic() + within
    while previews_location.read(data_dir).moving:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def go_on(data_dir: Path, *, now: float | None = None, waits: bool = False) -> str:
    """A move under way and not running (stopped by an error, or the API
    restarted): goes on in the background. With `waits` (upkeep), one that
    stopped on an error waits 15 minutes first. "started", "running",
    "waiting" or "not moving"."""
    if not previews_location.read(data_dir).moving:
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


# -- A library deleted for good -------------------------------------------------------


def forget_library(data_dir: Path, tenant_id: str, library_id: str) -> list[Path]:
    """A library deleted for good (its rows committed gone): its previews go
    from every folder previews may be in now (the server's, a library's own,
    where a move is carrying them from), then its own folder's entry in
    storage.json. Under the move lock, so no move carries its files
    meanwhile. A folder whose drive isn't plugged in keeps them, and while
    that's its own the entry stays too: the daily cleanup walks the folders
    storage.json names, and reports the library's as one the database
    doesn't list (remove-orphan-folders). Returns the folders removed.
    Raises MoveError when a move holds the lock: the files stay, reported
    the same way."""
    data_dir = Path(os.path.abspath(data_dir))
    if not (tenant_id.startswith(ACCOUNT_PREFIX) and library_id.startswith(previews_location.LIBRARY_PREFIX)
            and Path(tenant_id).name == tenant_id and Path(library_id).name == library_id):
        raise ValueError(f"Not an account and a library: {tenant_id!r}, {library_id!r}")
    removed: list[Path] = []
    with _one_at_a_time(data_dir, wait=LOOK_WAIT_SEC):
        location = previews_location.read(data_dir)
        for root in location.roots:
            folder = root / tenant_id / library_id
            if not location.available(data_dir, root) or not folder.is_dir() or folder.is_symlink():
                continue
            shutil.rmtree(folder)
            removed.append(folder)
        unplugged = [r for r in location.roots_for(library_id) if not location.available(data_dir, r)]
        if not unplugged and (library_id in location.libraries or library_id in location.libraries_moving_from):
            previews_location.write(
                data_dir, location.previews, location.moving_from,
                libraries={k: v for k, v in location.libraries.items() if k != library_id},
                libraries_moving_from={k: v for k, v in location.libraries_moving_from.items() if k != library_id})
    for folder in removed:
        logger.info("Library %s deleted for good: its previews in %s are gone", library_id, folder)
    return removed


# -- Moving ---------------------------------------------------------------------------


def _under_way(location: Location) -> tuple[Scope, Path, Path] | None:
    """The move under way: (what it carries, from, to)."""
    for library_id, old in sorted(location.libraries_moving_from.items()):
        return Scope(library_id), old, location.root_for(library_id)
    if location.moving_from is not None:
        return Scope(leaves=frozenset(location.libraries)), location.moving_from, location.previews
    return None


def run(data_dir: Path) -> None:
    """Moves what's left, here and now. Raises MoveError (recorded) when it stops."""
    data_dir = Path(os.path.abspath(data_dir))
    with _one_at_a_time(data_dir, wait=LOOK_WAIT_SEC):
        location = previews_location.read(data_dir)
        under_way = _under_way(location)
        if under_way is None:
            return
        scope, old, new = under_way
        _update_state(data_dir, error=None, failed_at=None)
        try:
            _both_there(data_dir, location, old, new)
            _move_all(data_dir, location, scope, old, new)
        except MoveError as exc:
            _stopped(data_dir, str(exc))
            raise
        except OSError as exc:
            said = _said(exc)
            _stopped(data_dir, said)
            raise MoveError(said) from exc
        _remove_empty(old, scope)
        now = previews_location.read(data_dir)
        if scope.library_id is None:
            previews_location.write(data_dir, new, None)
        else:
            previews_location.write(data_dir, now.previews, now.moving_from, libraries_moving_from={
                k: v for k, v in now.libraries_moving_from.items() if k != scope.library_id})
        _update_state(data_dir, finished_at=datetime.now(UTC).isoformat(), error=None, failed_at=None)
        logger.info("The previews are in %s now%s", new, f" (library {scope.library_id})" if scope.library_id else "")


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


def _both_there(data_dir: Path, location: Location, old: Path, new: Path) -> None:
    """Both folders' drives are plugged in. Checked as it goes: a file not
    found on a drive that went away isn't a file deleted meanwhile."""
    if not location.available(data_dir, new):
        raise MoveError(f"The new folder isn't there: {new}. Is its drive plugged in?")
    if not location.available(data_dir, old):
        raise MoveError(f"The old folder isn't there: {old}. Is its drive plugged in?")


def _move_all(data_dir: Path, location: Location, scope: Scope, old: Path, new: Path) -> None:
    state = read_state(data_dir)
    done_files, done_bytes = int(state.get("done_files") or 0), int(state.get("done_bytes") or 0)
    since = 0
    for path in _files(old, scope):
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
            _both_there(data_dir, location, old, new)
            if path.exists():
                raise
            continue  # deleted meanwhile
        done_files += 1
        done_bytes += size
        since += 1
        if since >= PROGRESS_EVERY:
            _both_there(data_dir, location, old, new)
            _update_state(data_dir, done_files=done_files, done_bytes=done_bytes)
            since = 0
    _both_there(data_dir, location, old, new)  # nothing listed because a drive went away isn't done
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


def _remove_empty(old: Path, scope: Scope) -> None:
    """The folders the move emptied: the scope's in each old account folder,
    then the account folder if nothing else is left in it."""
    for account in _accounts(old):
        top = account / scope.library_id if scope.library_id else account
        if not top.exists():
            continue
        for folder, _dirs, _names in sorted(os.walk(top), key=lambda w: len(w[0]), reverse=True):
            with contextlib.suppress(OSError):
                Path(folder).rmdir()
        with contextlib.suppress(OSError):
            account.rmdir()
