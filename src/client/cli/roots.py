"""Where a library's files live on this machine.

The database keeps each library's ingest root as the editing machine sees
it (for example /Volumes/media-01/Footage on the Mac Studio), because
exports point editors there. A machine that reaches the same storage at
another path, such as the brain mounting the DAS at /mnt/media-01, maps
the prefix in its own config (`lumiverb config map-root`), never in the
database (ADR-016 phase 2).
"""

from __future__ import annotations

import logging
import os
import threading
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

# A network mount whose server is asleep can block a stat for minutes.
DEFAULT_TIMEOUT_SEC = 10.0

# The latest probe thread per path, so a hung mount gets one stuck thread,
# not one per check.
_probes: dict[str, threading.Thread] = {}
_probes_lock = threading.Lock()


def _clean(path: str) -> str:
    """NFC, no trailing slash (except the filesystem root)."""
    path = unicodedata.normalize("NFC", path)
    return path.rstrip("/") or "/"


def map_root(root_path: str, root_map: dict[str, str]) -> str:
    """root_path with the longest matching prefix replaced.

    Prefixes match whole folders: /Volumes/media-01 maps
    /Volumes/media-01/Footage but not /Volumes/media-010. Matching is
    case-sensitive and ignores Unicode form and trailing slashes.
    """
    root = _clean(root_path)
    best: tuple[str, str] | None = None
    for src, dst in root_map.items():
        prefix = _clean(src)
        if prefix == "/":
            matches = root.startswith("/")
        else:
            matches = root == prefix or root.startswith(prefix + "/")
        if matches and (best is None or len(prefix) > len(best[0])):
            best = (prefix, _clean(dst))
    if best is None:
        return root
    prefix, target = best
    tail = root[len(prefix):].lstrip("/") if prefix != "/" else root.lstrip("/")
    if not tail:
        return target
    return f"{target.rstrip('/')}/{tail}"


def unmap_path(local_path: str, root_map: dict[str, str]) -> str:
    """The reverse of map_root: a path on this machine as library roots store it."""
    reverse = {dst: src for src, dst in root_map.items()}
    return map_root(local_path, reverse)


def _configured_map() -> dict[str, str]:
    from src.client.cli.config import load_config

    return load_config().root_map


def local_library_root(library: dict, root_map: dict[str, str] | None = None) -> Path | None:
    """The library's root on this machine, mapped but not checked."""
    root_path = library.get("root_path")
    if not root_path:
        return None
    mapping = _configured_map() if root_map is None else root_map
    return Path(map_root(root_path, mapping))


def _probe(path: Path, require_entries: bool) -> Path | None:
    """Resolve and list the folder. Runs in a thread: it can hang."""
    resolved = path.resolve()
    if not resolved.is_dir():
        return None
    if require_entries:
        with os.scandir(resolved) as entries:
            if next(entries, None) is None:
                return None
    return resolved


def reachable_root(
    library: dict,
    *,
    root_map: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    require_entries: bool = False,
) -> Path | None:
    """The library's root on this machine if it can be read now, else None.

    require_entries treats an empty folder as unreachable: an unmounted
    mount point is an empty folder, and scanning it would mark every file
    missing. The check gives up after `timeout` seconds, since a stat on a
    network mount whose server sleeps can block far longer.
    """
    path = local_library_root(library, root_map)
    if path is None:
        return None

    with _probes_lock:
        stuck = _probes.get(str(path))
        if stuck is not None and stuck.is_alive():
            # Still waiting on an earlier probe: don't add another stuck thread.
            logger.warning("Library root %s is still not answering; treating it as unreachable", path)
            return None

    result: dict[str, Path | None | BaseException] = {}

    def run() -> None:
        try:
            result["root"] = _probe(path, require_entries)
        except BaseException as exc:  # noqa: BLE001 — reported below
            result["error"] = exc

    worker = threading.Thread(target=run, name=f"root-probe-{library.get('library_id', '')}", daemon=True)
    with _probes_lock:
        _probes[str(path)] = worker
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        logger.warning("Library root %s didn't answer within %.0fs; treating it as unreachable", path, timeout)
        return None
    if "error" in result:
        logger.warning("Library root %s can't be read: %s", path, result["error"])
        return None
    root = result.get("root")
    return root if isinstance(root, Path) else None
