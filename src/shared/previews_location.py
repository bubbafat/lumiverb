"""Where an install's previews are: the folder its stored artifacts live in
(proxies, thumbnails, video previews, analysis proxies, scene frames, face
crops, playback cuts; every storage key, `<account>/<library>/...`).

DATA_DIR until an admin chooses another, on the machine itself (Settings ›
Storage); the choice is DATA_DIR/storage.json. Only the previews move: the
database, the search index, models and caches stay in DATA_DIR.

While a move runs, storage.json also says where they're moving from: new
files go to the new folder at once, a file not moved yet is read where it
was, and one deleted is deleted in both (src/server/storage/local.py,
src/server/storage/move.py). Shared: the scheduler never scans either
folder as a library's (src/processing/ingest.py).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

FILE = "storage.json"
# In a chosen folder (not DATA_DIR): that it's the one chosen, on its drive.
# Without it, the folder is an empty mount point (its drive unplugged), and
# nothing is written there: it would land on the system disk.
MARKER = ".lumiverb-previews"


@dataclass(frozen=True)
class Location:
    previews: Path  # where they are, and where new ones go
    moving_from: Path | None = None  # where those not moved yet still are

    def available(self, data_dir: str | Path, root: Path | None = None) -> bool:
        """The folder (the previews', or `root`) may be used: DATA_DIR (the
        install's own, made as needed), or a chosen one with its marker (its
        drive plugged in)."""
        root = root or self.previews
        if root == Path(os.path.abspath(data_dir)):
            return True
        return (root / MARKER).is_file()

    @property
    def roots(self) -> tuple[Path, ...]:
        return (self.previews,) if self.moving_from in (None, self.previews) else (self.previews, self.moving_from)


def read(data_dir: str | Path) -> Location:
    """The install's previews folder: storage.json's, else DATA_DIR."""
    data = Path(os.path.abspath(data_dir))
    try:
        said = json.loads((data / FILE).read_text())
    except (OSError, ValueError):
        said = {}
    if not isinstance(said, dict):
        said = {}

    def path(value: object) -> Path | None:
        return Path(value) if isinstance(value, str) and value.startswith("/") else None

    return Location(previews=path(said.get("previews")) or data, moving_from=path(said.get("moving_from")))


def write(data_dir: str | Path, previews: Path, moving_from: Path | None) -> Location:
    """Records where they are (DATA_DIR itself is written as no choice) and,
    during a move, where from. Atomic: readers see the old or the new."""
    data = Path(os.path.abspath(data_dir))
    value = {"previews": None if previews == data else str(previews),
             "moving_from": None if moving_from is None else str(moving_from)}
    target = data / FILE
    data.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".tmp")
    partial.write_text(json.dumps(value, indent=2))
    partial.replace(target)
    return read(data)
