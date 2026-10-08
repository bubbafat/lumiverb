"""IO utilities shared across the codebase."""

import os
import unicodedata
from pathlib import Path


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


def resolve_source_path(root: Path, rel_path: str) -> Path:
    """Where a library file lives on this machine's disk.

    rel_path is stored in Unicode NFC. Filesystems that don't normalize
    names (ext4, or NFS/SMB mounts on Linux) may hold the same name in NFD,
    which is how macOS often writes it, so fall back to the NFD form and
    then to matching each path component by its NFC form. Returns
    root / rel_path when nothing matches.
    """
    direct = root / rel_path
    if direct.exists():
        return direct
    nfd = root / unicodedata.normalize("NFD", rel_path)
    if nfd.exists():
        return nfd
    current = root
    for part in Path(rel_path).parts:
        candidate = current / part
        if not candidate.exists():
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
