"""The filesystem provider of the Store (ADR-020 decisions 2 and 8).

Keys are files under the previews folder: DATA_DIR, or one an admin chose,
or a library's own (src/shared/previews_location.py, storage.json), kept by
LocalStorage, whose folder may be on a drive that isn't plugged in (a chosen
folder without its `.lumiverb-previews` marker: its empty mount point). While
the previews move (src/server/storage/move.py), a key is read where it is
now, written to the new folder and deleted in both. All of that is this
provider's: a caller sees keys, NotFound and Unavailable, never a folder.

A file is never seen half written: write and writing put it beside its
place under a name of its own (PARTIAL and a random part, ending in the
suffix a tool reads the format from), which list never shows and a move
leaves alone while it's young, and rename it into place.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from src.server.storage.local import LocalStorage
from src.server.storage.store import NotFound, Unavailable, check_key

# What a write in progress is called while it's written: never a key.
PARTIAL = ".partial-"
# Every key is an account's: its first part is the account's folder.
ACCOUNT_PREFIX = "ten_"


class FileStore:
    """A Store on this machine's filesystem, over LocalStorage's folders."""

    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage

    # -- Where a key is ------------------------------------------------------------

    def _places(self, key: str) -> tuple[list[Path], list[Path]]:
        """Where the key may be: (its paths in the folders that can be reached,
        the folders that can't). A folder whose drive isn't plugged in is its
        empty mount point, on the system disk: nothing there is the key's."""
        location = self._storage.location
        data_dir = self._storage._data_dir
        roots = location.roots_for(location.library_of(check_key(key)))
        paths = self._storage.paths(key)
        reached = [path for root, path in zip(roots, paths, strict=True) if location.available(data_dir, root)]
        return reached, [root for root in roots if not location.available(data_dir, root)]

    def _found(self, key: str) -> Path:
        """The key's file where it is now. NotFound; Unavailable when it may be
        in a folder that can't be reached."""
        reached, unreachable = self._places(key)
        for path in reached:
            if path.is_file():
                return path
        if unreachable:
            raise Unavailable(f"Where {key} is kept can't be reached: {unreachable[0]}. Is its drive plugged in?")
        raise NotFound(f"No file for {key}")

    def _destination(self, key: str) -> Path:
        """Where the key's file goes. Unavailable when that folder can't be reached."""
        try:
            return self._storage.write_path(check_key(key))
        except FileNotFoundError as exc:  # LocalStorage: a chosen folder whose drive isn't plugged in
            raise Unavailable(str(exc)) from exc

    # -- Reading -------------------------------------------------------------------

    def open(self, key: str) -> BinaryIO:
        return open(self._found(key), "rb")

    def read(self, key: str) -> bytes:
        with self.open(key) as f:
            return f.read()

    def size(self, key: str) -> int:
        return self._found(key).stat().st_size

    def exists(self, key: str) -> bool:
        try:
            self._found(key)
        except NotFound:
            return False
        return True

    @contextlib.contextmanager
    def local_file(self, key: str) -> Iterator[Path]:
        """The file itself: on this filesystem there's nothing to fetch."""
        yield self._found(key)

    # -- Writing -------------------------------------------------------------------

    @contextlib.contextmanager
    def writing(self, key: str, suffix: str = "") -> Iterator[Path]:
        """A path no file is at yet (a tool that asks before writing over one
        doesn't ask), beside the key's place, ending in suffix."""
        path = self._destination(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f"{PARTIAL}{secrets.token_hex(8)}{suffix}")
        kept = partial
        try:
            yield partial
            # Where it goes now: a move may have begun (or ended) while it was written.
            path = self._destination(key)
            if path.parent != partial.parent:
                path.parent.mkdir(parents=True, exist_ok=True)
                kept = path.with_name(partial.name)
                shutil.move(partial, kept)  # another disk: copied beside its place, then renamed in
            os.replace(kept, path)
        finally:
            partial.unlink(missing_ok=True)
            kept.unlink(missing_ok=True)

    def write(self, key: str, data: bytes | BinaryIO) -> None:
        with self.writing(key) as partial, open(partial, "xb") as f:
            if isinstance(data, (bytes, bytearray, memoryview)):
                f.write(data)
            else:
                shutil.copyfileobj(data, f)

    def delete(self, key: str) -> None:
        reached, unreachable = self._places(key)
        for path in reached:
            path.unlink(missing_ok=True)
        if unreachable:
            raise Unavailable(f"{key} may be kept where it can't be reached: {unreachable[0]}. Is its drive plugged in?")

    # -- Listing -------------------------------------------------------------------

    def list(self, prefix: str) -> Iterator[str]:
        location = self._storage.location
        folder = PurePosixPath(prefix).parent if not prefix.endswith("/") else PurePosixPath(prefix)
        top = str(folder) if str(folder) != "." else ""
        if top:
            check_key(top)
        library = location.library_of(prefix)
        roots = location.roots_for(library) if library else location.roots
        if gone := [r for r in roots if not location.available(self._storage._data_dir, r)]:
            raise Unavailable(f"Some of {prefix or 'the keys'} can't be reached: {gone[0]}. Is its drive plugged in?")
        seen: set[str] = set()
        for root in roots:
            for account in ([root / top] if top else sorted(root.glob(f"{ACCOUNT_PREFIX}*"))):
                for dirpath, _dirs, names in os.walk(account):
                    for name in names:
                        if name.startswith(PARTIAL):
                            continue  # a write in progress
                        key = Path(dirpath, name).relative_to(root).as_posix()
                        if key.startswith(ACCOUNT_PREFIX) and key.startswith(prefix) and key not in seen:
                            seen.add(key)
                            yield key
