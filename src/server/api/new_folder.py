"""A folder an admin names that isn't there yet (the previews', a library's,
the backups'): the request says to make it (`make_folder`), or a 409
`make_folder` asks the user first, or a 400 says why it can't be made.

It's never made where a drive that isn't there would be (src/server/drives.py:
right in `/`, in `/Volumes` or a Linux desktop's `/media`, or under an fstab
mount point that isn't mounted). The 409 also says when it would be on the
data folder's drive.

Made only for a request that goes through: when what follows refuses the
folder (no room, inside the previews) or asks the user something more
(what moves), the folders it made are taken away again, and the request
that says yes makes them again."""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from src.server import drives
from src.server.api.errors import ApiError, DecisionRequiredError


def _named(raw: str | None) -> Path | None:
    """The folder `raw` names, when it's a full path with nothing there; else
    None (the caller's own check says what's wrong, if anything)."""
    if raw is None or not raw.strip():
        return None
    chosen = Path(raw.strip()).expanduser()
    if not chosen.is_absolute():
        return None
    chosen = Path(os.path.abspath(chosen))
    return None if os.path.lexists(chosen) else chosen


def _same_drive(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


@contextlib.contextmanager
def made_if_asked(raw: str | None, make: bool, data_dir: Path) -> Iterator[None]:
    """`raw` made, when it isn't there and the request says to make it, for
    the block; taken away again when the block raises. When it isn't there
    and the request doesn't say: a 409 make_folder with
    `{folder, free, same_drive_as_data}`. A 400 folder_not_usable when it
    can't be made."""
    folder = _named(raw)
    if folder is None:
        yield
        return
    if (why := drives.missing(folder)) is not None:
        raise ApiError(400, "folder_not_usable", why)
    there = drives.nearest(folder)
    if not os.access(there, os.W_OK | os.X_OK):
        raise ApiError(400, "folder_not_usable", f"Lumiverb can't make a folder in {there}.")
    if not make:
        same = _same_drive(there, data_dir)
        raise DecisionRequiredError(
            "make_folder",
            f"There's no folder at {folder}. Make it?"
            + (" It'd be on the same drive as Lumiverb's data." if same else ""),
            {"folder": str(folder), "free": shutil.disk_usage(there).free, "same_drive_as_data": same},
        )
    made = [folder, *(p for p in folder.parents if p != there and there in p.parents)]
    try:
        folder.mkdir(parents=True)
    except OSError as exc:
        raise ApiError(400, "folder_not_usable", f"Lumiverb couldn't make {folder}: {exc.strerror or exc}.") from None
    try:
        yield
    except BaseException:
        for path in made:  # deepest first; one with anything in it stays
            with contextlib.suppress(OSError):
                path.rmdir()
        raise
