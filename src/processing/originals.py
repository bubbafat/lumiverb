"""A library's originals, as this machine reads them (ADR-020 decision 5).

Originals are the user's files in a library's folder, so they aren't the
Store's (src/server/storage/store.py), though they have an interface of
their own in the same spirit. They differ from what Lumiverb keeps in four
ways: Lumiverb never writes them (ADR-016 principle 1); they're named by a
library and a path inside it, not by a key; this machine finds the
library's folder through its root map (src/processing/roots.py: the
scheduler's LUMIVERB_ROOT_MAP, the CLI's `lumiverb config map-root`); and
their drive may be unplugged or asleep.

- reach(library): the library's originals if they can be read here now,
  else None (an unmounted mount point, a share that doesn't answer, a root
  this machine refuses). What's read later is what was looked at: the
  folder reach found, not the root mapped again.
- Originals.walk(scope): what's in the library, or a folder of it, as names
  and facts (a scan's listing).
- Originals.stat(rel_path): a file's or folder's size, time and kind.
- Originals.local_file(rel_path): the file itself, for a tool, for the
  block. Never a copy, and nothing writes to it.
- where(library): where they'd be, as said to a person (nothing read).
- reader_named(): who this machine reads them as, as said to a person.

Nothing outside this module builds a path to an original or walks a
library's folder (tests/test_originals_only.py): the scheduler's look at
each library's storage (src/server/scheduler/account.py), its scans
(scheduler/scans.py, src/processing/scan.py), its producers' inputs
(scheduler/inputs.py), the API's question of which drive a library is on
(routers/libraries.py) and the CLI's scan all go through here. A path that
would leave the library is UnsafeRelPathError; a file that isn't there is
FileNotFoundError; one that can't be checked (a network mount's I/O error)
is another OSError, and is never taken for gone.

The scheduler reaches only the libraries this server reads
(src/shared/readers.py): a device's come through the API, and the server
never assumes an original exists (ADR-020 decision 9).

One provider: Folder, a folder on this machine's disk, which is where
originals are.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import pwd
import threading
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import Any, Protocol, runtime_checkable

from src.processing import roots
from src.shared.file_extensions import SUPPORTED_EXTENSIONS, media_type_of
from src.shared.io_utils import UnsafeRelPathError, resolve_source_path, stat_if_present
from src.shared.path_filter import PathFilter, has_control_character, is_path_included_merged

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stat:
    """A file's or folder's facts, as a scan compares them."""

    size: int
    mtime: datetime  # UTC
    folder: bool
    file: bool  # a regular file: what a library's media is


@runtime_checkable
class Originals(Protocol):
    """One library's originals, reached (reach): read only."""

    library_id: str

    @property
    def where(self) -> str:
        """Where they are, as said to a person."""

    def walk(self, scope: str | None = None, *, tenant_filters: list[PathFilter] | None = None,
             library_filters: list[PathFilter] | None = None, unlisted: list[str] | None = None,
             filtered: list[str] | None = None, refused_names: list[str] | None = None) -> list[dict]:
        """Every media file in the library, or in its folder `scope`: each
        {rel_path, file_size, file_mtime, media_type, ext}, rel_path in NFC.
        Media files the merged filters leave out are skipped (in `filtered`
        when given); a media file or folder whose name has a control
        character too (in `refused_names`, a folder's ending in "/"). Folders
        that can't be listed, or that hold a media file that can't be
        checked, go in `unlisted` ("" is the library), so nothing in them is
        taken for gone."""

    def stat(self, rel_path: str) -> Stat | None:
        """The file or folder at rel_path ("" is the library), or None when
        nothing is there."""

    def local_file(self, rel_path: str) -> contextlib.AbstractContextManager[Path]:
        """The file at rel_path as a path on this machine, for the block.
        FileNotFoundError when no file is there."""

    def device(self, *, timeout: float) -> int | None:
        """The drive they're on here, or None when that isn't known within
        `timeout` seconds."""


def reach(library: Mapping[str, Any], *, require_entries: bool = False, timeout: float = roots.DEFAULT_TIMEOUT_SEC,
          root_map: dict[str, str] | None = None) -> Originals | None:
    """The library's originals if they can be read on this machine now, else
    None (roots.reachable_root: within `timeout`, never a root this machine
    refuses, nor one outside its account's folders when the library carries
    them; require_entries takes an empty folder, an unmounted mount point,
    for unreachable). The scheduler never reaches a library a device
    reads."""
    from src.processing.machine import current
    from src.shared.readers import read_by_server

    if current().mapped_roots_only and not read_by_server(library):
        return None
    root = roots.reachable_root(dict(library), root_map=root_map, timeout=timeout, require_entries=require_entries)
    return None if root is None else Folder(root, str(library.get("library_id", "")))


def where(library: Mapping[str, Any], root_map: dict[str, str] | None = None) -> Path | None:
    """Where the library's originals would be on this machine (its root,
    mapped), as said to a person: nothing is read, and nothing is reached."""
    return roots.local_library_root(dict(library), root_map)


def reader_named() -> str:
    """Who this machine reads files as, for a person to give them to: "the lumiverb user"."""
    try:
        return f"the {pwd.getpwuid(os.geteuid()).pw_name} user"
    except KeyError:
        return f"user {os.geteuid()}"


# A drive's look still stuck, per folder (Folder.device): while one is, no
# other starts, so threads don't pile up on a share that sleeps.
_device_looks: dict[str, threading.Thread] = {}


class Folder:
    """A library's originals in a folder on this machine's disk: the root
    reach found, mapped, resolved and looked at."""

    def __init__(self, root: Path, library_id: str = "") -> None:
        self._root = root
        self.library_id = library_id

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Folder) and (self._root, self.library_id) == (other._root, other.library_id)

    def __hash__(self) -> int:
        return hash((self._root, self.library_id))

    def __repr__(self) -> str:
        return f"Folder({str(self._root)!r}, {self.library_id!r})"

    @property
    def where(self) -> str:
        return str(self._root)

    def stat(self, rel_path: str) -> Stat | None:
        st = stat_if_present(resolve_source_path(self._root, rel_path))
        if st is None:
            return None
        return Stat(size=st.st_size, mtime=datetime.fromtimestamp(st.st_mtime, tz=UTC),
                    folder=S_ISDIR(st.st_mode), file=S_ISREG(st.st_mode))

    @contextlib.contextmanager
    def local_file(self, rel_path: str) -> Iterator[Path]:
        path = resolve_source_path(self._root, rel_path)
        st = stat_if_present(path)
        if st is None or not S_ISREG(st.st_mode):
            raise FileNotFoundError(errno.ENOENT, "No file there in the library", rel_path)
        yield path

    def device(self, *, timeout: float) -> int | None:
        """Asked in a request: a share whose server sleeps (prod's CIFS mount
        of the Mac's drives) can block a stat for minutes, so it's looked at
        in a thread, and not known after `timeout`."""
        key = str(self._root)
        stuck = _device_looks.get(key)
        if stuck is not None and stuck.is_alive():
            return None
        found: dict[str, int] = {}

        def look() -> None:
            with contextlib.suppress(OSError):  # not there now: not known
                found["dev"] = os.stat(self._root).st_dev

        worker = threading.Thread(target=look, name=f"originals-stat-{self.library_id}", daemon=True)
        _device_looks[key] = worker
        worker.start()
        worker.join(timeout)
        return found.get("dev")

    def walk(self, scope: str | None = None, *, tenant_filters: list[PathFilter] | None = None,
             library_filters: list[PathFilter] | None = None, unlisted: list[str] | None = None,
             filtered: list[str] | None = None, refused_names: list[str] | None = None) -> list[dict]:
        """Hidden files are listed; links aren't followed, and the server's
        own folders (its data and previews, which may be on a library's
        drive, even inside its folder) are never gone into."""
        root = self._root
        walk_root = root
        if scope:
            try:
                walk_root = resolve_source_path(root, scope)
            except UnsafeRelPathError:
                logger.warning("Not scanning %s: it leaves the library", scope)
                if unlisted is not None:
                    unlisted.append(scope)  # its files aren't taken for deleted
                return []

        def unreadable(exc: OSError, folder: str | None = None) -> None:
            folder = folder or exc.filename or str(walk_root)
            logger.warning("Can't list %s: %s", folder, exc)
            if unlisted is not None:
                rel = os.path.relpath(folder, root)
                rel = "" if rel == "." else unicodedata.normalize("NFC", rel)
                if rel not in unlisted:
                    unlisted.append(rel)

        has_filters = bool(tenant_filters or library_filters)
        t_filters = tenant_filters or []
        l_filters = library_filters or []

        refused = _refused_folders()
        found: list[Path] = []
        for folder, dirs, names in os.walk(walk_root, onerror=unreadable):
            if refused:
                dirs[:] = [d for d in dirs if not _is_refused(Path(folder, d), refused)]
            dirs[:] = [d for d in dirs if not _control_named(root, Path(folder, d), refused_names, folder=True)]
            found.extend(Path(folder, name) for name in names)

        results = []
        for p in sorted(found):
            # Only files the scan would take are stat'ed: one that can't be
            # checked holds back deletions in its folder.
            ext = p.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS or _control_named(root, p, refused_names):
                continue

            # NFC, as the macOS app's scan stored it (before the server scanned
            # every library): one identity per file whichever machine scans it.
            # Reading one finds it in either form (resolve_source_path).
            rel_path = unicodedata.normalize("NFC", str(p.relative_to(root)))

            if has_filters and not is_path_included_merged(rel_path, t_filters, l_filters):
                if filtered is not None:
                    filtered.append(rel_path)
                continue

            try:
                st = stat_if_present(p, follow_symlinks=False)  # a symlink isn't S_ISREG
            except OSError as exc:
                unreadable(exc, str(p.parent))
                continue
            if st is None or not S_ISREG(st.st_mode):
                continue

            results.append({
                "rel_path": rel_path,
                "file_size": st.st_size,
                "file_mtime": datetime.fromtimestamp(st.st_mtime, tz=UTC),
                "media_type": media_type_of(ext),
                "ext": ext,
            })

        return results


def _refused_folders() -> dict[str, set[tuple[int, int]]]:
    """The folders a library walk never goes into (the server's data and its
    previews, which may be on a library's drive, even inside its folder), as
    {lowercased name: {(device, inode)}}: matched by what they are, whatever
    the path's spelling (a case-insensitive disk) or the links to them."""
    from src.processing.machine import current

    out: dict[str, set[tuple[int, int]]] = {}
    for folder in current().refused_here():
        try:
            st = os.stat(folder)
        except OSError:
            continue
        out.setdefault(Path(folder).name.lower(), set()).add((st.st_dev, st.st_ino))
    return out


def _is_refused(path: Path, refused: dict[str, set[tuple[int, int]]]) -> bool:
    same_name = refused.get(path.name.lower())
    if not same_name:
        return False
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) in same_name


def _control_named(root: Path, path: Path, refused: list[str] | None = None, *, folder: bool = False) -> bool:
    """A file or folder whose name has a control character (a newline, a
    tab): never scanned, nor anything in a folder so named. Said once, as a
    warning (repr: the name made printable); it isn't a failure to try again.
    Its rel path goes in `refused` when given (a folder's ends in "/")."""
    if not has_control_character(path.name):
        return False
    rel = unicodedata.normalize("NFC", str(path.relative_to(root)))
    logger.warning("Not scanning %r: its name has a control character", rel)
    if refused is not None:
        refused.append(rel + "/" if folder else rel)
    return True
