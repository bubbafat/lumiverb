"""IO utilities shared across the codebase."""

import errno
import os
import unicodedata
from pathlib import Path

# A stat failing with these means the path isn't there.
_ABSENT = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)


def stat_if_present(path: str | os.PathLike) -> os.stat_result | None:
    """os.stat, or None if the path isn't there.

    Other errors (no permission, an I/O error on a network mount) raise: a
    path that can't be checked mustn't be taken for gone. Python 3.14's
    Path.exists() and is_file() say False for those too.
    """
    try:
        return os.stat(path)
    except OSError as exc:
        if exc.errno in _ABSENT:
            return None
        raise


def file_non_empty(path: Path, *, min_bytes: int = 1) -> bool:
    """Return True if path exists and has at least min_bytes. Catches OSError."""
    try:
        return path.exists() and path.stat().st_size >= min_bytes
    except OSError:
        return False


def normalize_path_prefix(path: str | None) -> str | None:
    """
    Normalize a path prefix for consistent DB filtering.
    - Converts backslashes to forward slashes
    - Strips leading/trailing slashes and whitespace
    - Unicode NFC, like stored rel_paths (macOS tab completion gives NFD)
    - Returns None if the result is empty
    """
    if not path:
        return None
    normalized = unicodedata.normalize("NFC", path.replace("\\", "/").strip().strip("/"))
    return normalized or None



def normalize_rel_path(rel_path: str) -> str:
    """A library-relative path in its one stored form: Unicode NFC.

    macOS hands out decomposed (NFD) names; without this, the same file
    scanned from macOS and Linux would be two assets.
    """
    return unicodedata.normalize("NFC", rel_path)


class UnsafeRelPathError(ValueError):
    """A library-relative path that would leave the library root."""


def check_rel_path(rel_path: str, *, allow_root: bool = False) -> str:
    """rel_path as stored (NFC), or UnsafeRelPathError unless it stays inside
    the library: relative (no leading "/"), no ".." part, no NUL. "" is the
    library root, allowed only with allow_root. Every write that takes a
    rel_path checks it here, and resolve_source_path does too."""
    if not isinstance(rel_path, str) or "\x00" in rel_path:
        raise UnsafeRelPathError(f"Invalid rel_path: {rel_path!r}")
    if rel_path == "":
        if allow_root:
            return rel_path
        raise UnsafeRelPathError("rel_path is empty")
    if rel_path.startswith("/") or ".." in rel_path.split("/"):
        raise UnsafeRelPathError(f"rel_path must stay inside the library: {rel_path!r}")
    return normalize_rel_path(rel_path)


def is_within(rel_path: str, folder: str | None) -> bool:
    """rel_path is the library folder or inside it. None or "" is the whole library."""
    return not folder or rel_path == folder or rel_path.startswith(folder + "/")


def resolve_source_path(root: Path, rel_path: str) -> Path:
    """Where a library file lives on this machine's disk.

    rel_path is stored in Unicode NFC. Filesystems that don't normalize
    names (ext4, or NFS/SMB mounts on Linux) may hold the same name in NFD,
    which is how macOS often writes it, so fall back to the NFD form and
    then to matching each path component by its NFC form. Returns
    root / rel_path when nothing matches; raises OSError when a path can't
    be checked (stat_if_present), UnsafeRelPathError for a rel_path that
    would leave root (absolute, or with a ".." part).
    """
    check_rel_path(rel_path, allow_root=True)  # never a path outside root
    direct = root / rel_path
    if stat_if_present(direct) is not None:
        return direct
    nfd = root / unicodedata.normalize("NFD", rel_path)
    if stat_if_present(nfd) is not None:
        return nfd
    current = root
    for part in Path(rel_path).parts:
        candidate = current / part
        if stat_if_present(candidate) is None:
            want = unicodedata.normalize("NFC", part)
            try:
                names = os.listdir(current)
            except OSError:
                return direct
            match = next((n for n in names if unicodedata.normalize("NFC", n) == want), None)
            if match is None:
                return direct
            candidate = current / match
        current = candidate
    return current
