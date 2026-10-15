"""What a Mac install keeps is its user's alone (lumiverb-server run).

run makes everything with umask 077, so what it and its children write
(PostgreSQL's, Quickwit's index, previews, caches, the log once it's
rotated) is readable by this user only, and the same after an upgrade's
re-exec. At each start it also makes private what an earlier install left
open: DATA_DIR, each account's folder in a previews folder chosen elsewhere
(never the chosen folder itself: it may be a drive's), and the service log
with its rotated files. Only what this user owns is changed, and each change
is logged. Backups make themselves private already (backups.py).
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

UMASK = 0o077
# The accounts' folders in a previews folder ({previews}/{tenant_id}/..., storage/local.py).
ACCOUNT_PREFIX = "ten_"


def paths_to_keep_private(data: Path, log_file: Path | None) -> list[Path]:
    """DATA_DIR, the accounts' folders in every previews folder outside it, and the log with its rotated files."""
    from src.shared import previews_location

    found = [data]
    for root in previews_location.read(data).roots:
        if root == data or data in root.parents:
            continue
        try:
            found += sorted(p for p in root.iterdir() if p.name.startswith(ACCOUNT_PREFIX) and p.is_dir())
        except OSError:  # a drive that isn't there: nothing to change
            continue
    if log_file is not None:
        found += [log_file, *sorted(log_file.parent.glob(f"{log_file.name}.[0-9]*"))]
    return list(dict.fromkeys(found))


def make_private(paths: Iterable[Path]) -> list[tuple[Path, int, int]]:
    """Takes group and others' access away from each path this user owns.
    Returns (path, mode before, mode after) for each one changed."""
    changed = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        mode = stat.S_IMODE(st.st_mode)
        if st.st_uid != os.geteuid() or not mode & 0o077:
            continue
        try:
            path.chmod(mode & ~0o077)
        except OSError as exc:
            logger.warning("Couldn't make %s private: %s", path, exc)
            continue
        changed.append((path, mode, mode & ~0o077))
    return changed


def begin(data: Path, log_file: Path | None) -> None:
    """run's first step: umask 077 for everything after, then what's there made private."""
    os.umask(UMASK)
    for path, before, after in make_private(paths_to_keep_private(data, log_file)):
        logger.info("Made %s private (%o -> %o)", path, before, after)
