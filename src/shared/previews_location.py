"""Where an install's previews are: the folder its stored artifacts live in
(proxies, thumbnails, video previews, analysis proxies, scene frames, face
crops, playback cuts; every storage key, `<account>/<library>/...`).

DATA_DIR until an admin chooses another, on the machine itself (Settings ›
Storage); a library can have a folder of its own instead (its settings).
The choices are DATA_DIR/storage.json. Only the previews move: the
database, the search index, models and caches stay in DATA_DIR.

While a move runs, storage.json also says where they're moving from: new
files go to the new folder at once, a file not moved yet is read where it
was, and one deleted is deleted in both (src/server/storage/local.py,
src/server/storage/move.py). Shared: the scheduler never scans any of
these folders as a library's (src/processing/ingest.py).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

FILE = "storage.json"
# In a chosen folder (not DATA_DIR): that it's the one chosen, on its drive.
# Without it, the folder is an empty mount point (its drive unplugged), and
# nothing is written there: it would land on the system disk.
MARKER = ".lumiverb-previews"
LIBRARY_PREFIX = "lib_"


@dataclass(frozen=True)
class Location:
    previews: Path  # where they are, and where new ones go (every library without a folder of its own)
    moving_from: Path | None = None  # where those not moved yet still are
    # A library's own folder, instead of `previews`.
    libraries: dict[str, Path] = field(default_factory=dict)
    # A library being moved: where its previews not moved yet still are.
    libraries_moving_from: dict[str, Path] = field(default_factory=dict)

    def available(self, data_dir: str | Path, root: Path | None = None) -> bool:
        """The folder (the previews', or `root`) may be used: DATA_DIR (the
        install's own, made as needed), or a chosen one with its marker (its
        drive plugged in)."""
        root = root or self.previews
        if root == Path(os.path.abspath(data_dir)):
            return True
        return (root / MARKER).is_file()

    @staticmethod
    def library_of(key: str) -> str | None:
        """The library a storage key is a file of (its second part)."""
        parts = key.split("/", 2)
        return parts[1] if len(parts) > 1 and parts[1].startswith(LIBRARY_PREFIX) else None

    def root_for(self, library_id: str | None) -> Path:
        """Where a library's previews go (an account's own files: `previews`)."""
        return self.libraries.get(library_id, self.previews) if library_id else self.previews

    def from_for(self, library_id: str | None) -> Path | None:
        """Where a library's previews not moved yet still are, during a move."""
        if library_id and library_id in self.libraries_moving_from:
            return self.libraries_moving_from[library_id]
        if library_id and library_id in self.libraries:
            return None  # its own folder: the server-wide move doesn't touch it
        return self.moving_from

    def roots_for(self, library_id: str | None) -> tuple[Path, ...]:
        """Where a library's previews may be: its folder, then (moving) the old one."""
        root, old = self.root_for(library_id), self.from_for(library_id)
        return (root,) if old in (None, root) else (root, old)

    @property
    def moving(self) -> bool:
        return self.moving_from is not None or bool(self.libraries_moving_from)

    @property
    def roots(self) -> tuple[Path, ...]:
        """Every folder previews may be in now, the server-wide one first."""
        found = [self.previews, *(r for r in (self.moving_from,) if r is not None),
                 *self.libraries.values(), *self.libraries_moving_from.values()]
        return tuple(dict.fromkeys(found))


def _path(value: object) -> Path | None:
    return Path(value) if isinstance(value, str) and value.startswith("/") else None


def _paths(value: object) -> dict[str, Path]:
    if not isinstance(value, dict):
        return {}
    return {k: p for k, v in value.items() if isinstance(k, str) and k.startswith(LIBRARY_PREFIX)
            and (p := _path(v)) is not None}


def read(data_dir: str | Path) -> Location:
    """The install's previews folders: storage.json's, else DATA_DIR."""
    data = Path(os.path.abspath(data_dir))
    try:
        said = json.loads((data / FILE).read_text())
    except (OSError, ValueError):
        said = {}
    if not isinstance(said, dict):
        said = {}
    return Location(previews=_path(said.get("previews")) or data, moving_from=_path(said.get("moving_from")),
                    libraries=_paths(said.get("libraries")),
                    libraries_moving_from=_paths(said.get("libraries_moving_from")))


def write(data_dir: str | Path, previews: Path, moving_from: Path | None, *,
          libraries: dict[str, Path] | None = None, libraries_moving_from: dict[str, Path] | None = None) -> Location:
    """Records where they are (DATA_DIR itself is written as no choice), and
    during a move, where from. The libraries' folders are kept as they were
    unless given. Atomic: readers see the old or the new."""
    data = Path(os.path.abspath(data_dir))
    now = read(data)
    libraries = now.libraries if libraries is None else libraries
    libraries_moving_from = now.libraries_moving_from if libraries_moving_from is None else libraries_moving_from
    value = {"previews": None if previews == data else str(previews),
             "moving_from": None if moving_from is None else str(moving_from),
             "libraries": {k: str(v) for k, v in sorted(libraries.items())},
             "libraries_moving_from": {k: str(v) for k, v in sorted(libraries_moving_from.items())}}
    target = data / FILE
    data.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".tmp")
    partial.write_text(json.dumps(value, indent=2))
    partial.replace(target)
    return read(data)
