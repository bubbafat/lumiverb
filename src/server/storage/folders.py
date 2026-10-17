"""The filesystem provider's folders: what it keeps besides keys (ADR-020
decision 8).

The daily sweep removes the files no row names through the Store, key by
key (src/server/search/cleanup.py). What a filesystem has that a listing of
keys doesn't show is this provider's, and here:

- the folders of libraries and accounts the database doesn't list. The
  sweep reports them and never removes them: a database restored from an
  older backup looks just like that (Robert). They go when an admin names
  them (remove-orphan-folders), from every folder previews may be in, with
  the mover's forget_library and forget_account (under its lock);
- partial writes a crash left (files.PARTIAL): a write that ends removes
  its own, and no listing shows one;
- a move under way, which the sweep waits for: files are in two folders.

A folder is named by its key prefix (`{tenant}` or `{tenant}/{library}`),
never by where it is.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from src.server.storage import local, move
from src.server.storage.files import ACCOUNT_PREFIX, PARTIAL
from src.shared.previews_location import LIBRARY_PREFIX

logger = logging.getLogger(__name__)

MOVING = "The previews are moving: cleanup waits until they're moved."
# A partial write untouched this long was left by a crash: one in progress is still being written.
LEFT_AFTER_SEC = 3600


def why_waiting() -> str | None:
    """Why the sweep doesn't run now, or None: the previews are moving."""
    return MOVING if local.get_storage().location.moving else None


def _reached() -> tuple[Path, list[Path]]:
    """DATA_DIR, and every folder previews may be in now that can be reached
    (a chosen folder whose drive isn't plugged in is its empty mount point)."""
    storage = local.get_storage()
    location = storage.location
    data_dir = storage._data_dir
    return Path(os.path.abspath(data_dir)), [r for r in location.roots if location.available(data_dir, r)]


def _named(folder: Path, prefix: str) -> set[str]:
    """The folders in folder named prefix…, never a link (an operator's: never removed)."""
    try:
        return {e.name for e in os.scandir(folder) if e.name.startswith(prefix) and e.is_dir(follow_symlinks=False)}
    except OSError:
        return set()


def accounts() -> set[str]:
    """The accounts with a folder, in the folders that can be reached."""
    return set().union(*(_named(root, ACCOUNT_PREFIX) for root in _reached()[1]))


def libraries(tenant_id: str) -> set[str]:
    """The account's libraries with a folder, in the folders that can be reached."""
    return set().union(*(_named(root / tenant_id, LIBRARY_PREFIX) for root in _reached()[1]))


def _there(prefix: str) -> list[Path]:
    return [root / prefix for root in _reached()[1]
            if (root / prefix).is_dir() and not (root / prefix).is_symlink()]


def _measured(prefix: str, paths: list[Path]) -> dict:
    """A folder as it's reported: its id (its last part), its prefix, its
    size and its newest mtime, across every folder it's in."""
    size = 0
    newest = 0.0
    for path in paths:
        for root, dirs, files in os.walk(path):
            for name in [*dirs, *files, ""]:
                try:
                    st = (Path(root) / name).stat()
                except OSError:
                    continue
                newest = max(newest, st.st_mtime)
                if name in files:
                    size += st.st_size
    return {
        "folder_id": prefix.rsplit("/", 1)[-1],
        "path": prefix,
        "bytes": size,
        "newest_mtime": datetime.fromtimestamp(newest, tz=UTC).isoformat() if newest else None,
    }


def measured(prefix: str) -> dict:
    """The folder of an account (`ten_…`) or one of its libraries
    (`ten_…/lib_…`), as reported."""
    return _measured(prefix, _there(prefix))


def remove(prefix: str, still_unlisted: Callable[[], bool], *, dry_run: bool) -> dict | None:
    """An account's or a library's folder the database doesn't list, from
    every folder it's in, after checking again that it isn't listed (a
    restore may have brought its row back). The folder as it was, or None
    when there's no such folder or it's listed now. Raises MoveError when a
    move holds the lock: nothing is removed."""
    paths = _there(prefix)
    if not paths:
        return None
    if not still_unlisted():
        logger.info("remove-orphan-folders: %s is listed now; kept", prefix)
        return None
    folder = _measured(prefix, paths)
    logger.warning("remove-orphan-folders: %s %s (%d bytes)", "would remove" if dry_run else "removing",
                   prefix, folder["bytes"])
    if not dry_run:
        data_dir = _reached()[0]
        tenant_id, _, library_id = prefix.partition("/")
        if library_id:
            move.forget_library(data_dir, tenant_id, library_id)
        else:
            move.forget_account(data_dir, tenant_id)
    return folder


def left_by_a_crash(tenant_id: str, library_ids: Iterable[str], *, dry_run: bool) -> tuple[int, int]:
    """Partial writes in the libraries' folders untouched for LEFT_AFTER_SEC,
    removed (a dry run counts them): (files, bytes)."""
    cutoff = time.time() - LEFT_AFTER_SEC
    count = size = 0
    library_ids = list(library_ids)
    for root in _reached()[1]:
        for library_id in library_ids:
            for dirpath, _dirs, names in os.walk(root / tenant_id / library_id):
                for name in names:
                    if not name.startswith(PARTIAL):
                        continue
                    path = Path(dirpath, name)
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    if st.st_mtime > cutoff:
                        continue
                    logger.info("%s a write a crash left: %s (%d bytes)",
                                "Would remove" if dry_run else "Removing", path, st.st_size)
                    if not dry_run:
                        path.unlink(missing_ok=True)
                    count += 1
                    size += st.st_size
    return count, size
