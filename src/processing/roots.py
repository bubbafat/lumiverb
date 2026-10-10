"""Where a library's files live on this machine.

The database keeps each library's ingest root as the editing machine sees
it (for example /Volumes/media-01/Footage on the Mac Studio), because
exports point editors there. A machine that reaches the same storage at
another path, such as the brain mounting the DAS at /mnt/media-01, maps
the prefix in its own settings (src/processing/machine.py: the scheduler's
LUMIVERB_ROOT_MAP, the CLI's `lumiverb config map-root`), never in the
database (ADR-016 phase 2).
"""

from __future__ import annotations

import logging
import os
import re
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
    from src.processing.machine import current

    return dict(current().root_map)


def _maps(root_path: str, root_map: dict[str, str]) -> bool:
    """Whether a prefix in root_map covers root_path (as map_root matches)."""
    root = _clean(root_path)
    for src in root_map:
        prefix = _clean(src)
        if prefix == "/" or root == prefix or root.startswith(prefix + "/"):
            return True
    return False


def _refused(library: dict, mapping: dict[str, str]) -> str | None:
    """Why the library's root mustn't be read here, before looking at the disk."""
    from src.processing.machine import current

    if not library["root_path"].startswith("/"):
        return "root isn't absolute"
    if current().mapped_roots_only and not _maps(library["root_path"], mapping):
        return "root isn't under the root map"
    return None


def _outside(resolved: Path, mapping: dict[str, str]) -> str | None:
    """Why the library's resolved root mustn't be read here. On disk: run
    in the probe's thread."""
    from src.processing.machine import current

    here = current()
    if any(resolved.is_relative_to(Path(d).resolve()) for d in here.refused_roots):
        return "root is in the server's data"
    if here.mapped_roots_only and not any(resolved.is_relative_to(Path(t).resolve()) for t in mapping.values()):
        return "root leaves the root map"
    return None


# Refusals already logged, so each is said once, not on every look.
_said: set[tuple[str, str]] = set()


def _refuse(library: dict, reason: str) -> None:
    key = (str(library.get("library_id", "")), reason)
    if key not in _said:
        _said.add(key)
        logger.warning("Library %s (%s) refused: %s: %s", library.get("name", ""),
                       library.get("library_id", ""), reason, library.get("root_path"))


def local_library_root(library: dict, root_map: dict[str, str] | None = None) -> Path | None:
    """The library's root on this machine, mapped but not checked."""
    root_path = library.get("root_path")
    if not root_path:
        return None
    mapping = _configured_map() if root_map is None else root_map
    return Path(map_root(root_path, mapping))


# Mount points the system expects (the brain mounts the DAS from here).
FSTAB = Path("/etc/fstab")


def _expected_mount(path: Path) -> Path | None:
    """The deepest mount point fstab lists at or above `path`, other than /."""
    try:
        lines = FSTAB.read_text().splitlines()
    except OSError:
        return None
    best: Path | None = None
    for line in lines:
        fields = line.split()
        if len(fields) < 2 or fields[0].startswith("#") or fields[1] in ("/", "none", "swap"):
            continue
        # fstab escapes a space as \040, a tab as \011 and so on.
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[1]))
        if (path == mount or mount in path.parents) and (best is None or len(mount.parts) > len(best.parts)):
            best = mount
    return best


def _probe(path: Path, require_entries: bool) -> Path | None:
    """Resolve and list the folder. Runs in a thread: it can hang."""
    # A folder fstab says is a mount counts only while it's mounted: anything
    # in an unmounted mount point (a stray copy) isn't the share.
    resolved = path.resolve()
    for candidate in dict.fromkeys((path, resolved)):  # a root map may reach the mount through a symlink
        mount = _expected_mount(candidate)
        if mount is not None and not os.path.ismount(mount):
            return None
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

    A root that isn't absolute is never read, nor one in the server's data
    (the machine's refused_roots). Where the machine reads mapped roots only
    (the scheduler), a root not under root_map, or resolving outside its
    targets, is refused too.

    require_entries treats an empty folder as unreachable: an unmounted
    mount point is an empty folder, and scanning it would mark every file
    missing. The check gives up after `timeout` seconds, since a stat on a
    network mount whose server sleeps can block far longer.
    """
    mapping = _configured_map() if root_map is None else root_map
    path = local_library_root(library, mapping)
    if path is None:
        return None
    if (reason := _refused(library, mapping)) is not None:
        _refuse(library, reason)
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
            root = _probe(path, require_entries)
            if root is not None and (reason := _outside(root, mapping)) is not None:
                _refuse(library, reason)
                root = None
            result["root"] = root
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
