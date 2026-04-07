"""IO utilities shared across the codebase."""

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
    - Returns None if the result is empty
    """
    if not path:
        return None
    normalized = path.replace("\\", "/").strip().strip("/")
    return normalized or None

