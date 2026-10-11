"""Whether a chosen folder's drive is there (the previews', a library's, the
backups'), before Lumiverb writes to it or makes it: an unplugged drive
leaves a folder on the system disk in its place, or nothing at all.

- A mount point /etc/fstab lists (a Linux box's /mnt/ssd2, say, with
  `nofail`) stays as an empty folder when its drive isn't mounted.
- A Mac's drives appear in /Volumes, a Linux desktop's in /media,
  /media/<user> or /run/media/<user>: each drive's folder goes when it's
  unplugged, so a folder made there would stand in for it.
- Nothing is made right in /.
"""

from __future__ import annotations

import os
from pathlib import Path

from src.processing import roots

# Folders whose subfolders are drives (a Mac's, a Linux desktop's).
DRIVE_FOLDERS = (Path("/Volumes"), Path("/media"), Path("/run/media"))
# Folders whose subfolders hold a user's drives (/media/<user>, /run/media/<user>).
USER_DRIVE_FOLDERS = (Path("/media"), Path("/run/media"))


def nearest(path: Path) -> Path:
    """`path` when it's a folder, else the deepest folder above it that is."""
    return next((p for p in (path, *path.parents) if p.is_dir()), Path("/"))


def missing(path: Path) -> str | None:
    """Why `path`'s drive isn't there, said for the user; None when it is
    (or `path` isn't on a drive that comes and goes). `path` is a full
    path; it needn't be there yet."""
    for candidate in dict.fromkeys((path, path.resolve())):  # reached through a symlink
        mount = roots.expected_mount(candidate)
        if mount is not None and not os.path.ismount(mount):
            return f"There's no drive at {mount}. Is it mounted?"
    there = nearest(path)
    if there == path:
        return None
    drive = there / path.relative_to(there).parts[0]
    if there == Path("/"):
        return f"There's no drive at {drive}. Is it plugged in?"
    where_drives_go = there in DRIVE_FOLDERS or there.parent in USER_DRIVE_FOLDERS
    if where_drives_go and not os.path.ismount(there):
        return f"There's no drive at {drive}. Is it plugged in?"
    return None
