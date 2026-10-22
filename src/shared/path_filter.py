"""Shared path filter evaluation for library ingest. Pure functions, no I/O."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Literal

# C0 controls (U+0000-U+001F: a newline, a tab, NUL) and DEL (U+007F).
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def has_control_character(name: str) -> bool:
    """name (a file or folder name, or a path) has a control character: C0
    (U+0000-U+001F: a newline, a tab, NUL) or DEL (U+007F). No library takes
    such a name: scans skip it and the server refuses it (check_rel_path)."""
    return _CONTROL.search(name) is not None


@dataclass
class PathFilter:
    type: Literal["include", "exclude"]
    pattern: str


def _glob_match(pattern: str, path: str) -> bool:
    """
    Case-insensitive glob match supporting ** for cross-segment matching.
    Both pattern and path are lowercased before comparison.
    * and ? match within a single path segment; ** matches zero or more segments.

    Worked out a pattern segment at a time, last to first, over every place in
    the path at once: steps grow with the pattern's segments times the path's,
    however many ** it has (trying each way a ** could go, as this once did,
    took twice as long for each more).
    """
    pl = pattern.lower().replace("\\", "/")
    path_l = path.lower().replace("\\", "/")
    # Preserve empty path as single empty segment for edge cases
    path_segs = path_l.split("/") if path_l else [""]
    pattern_segs = pl.split("/")
    n = len(path_segs)
    empty = path_segs == [""]
    # Whether pattern_segs[j:] is all ** (what's left of a pattern once the path is used up).
    stars_from = [True] * (len(pattern_segs) + 1)
    for j in range(len(pattern_segs) - 1, -1, -1):
        stars_from[j] = stars_from[j + 1] and pattern_segs[j] == "**"

    # rest[pi]: whether path_segs[pi:] matches the pattern's segments after the one at hand. With none left,
    # the path must be used up too (an empty path matches nothing left as well).
    rest = [pi >= n or (pi == 0 and empty) for pi in range(n + 1)]
    for j in range(len(pattern_segs) - 1, -1, -1):
        pseg = pattern_segs[j]
        here = [False] * (n + 1)
        later = False  # whether rest matches from pi or any place after it: where a ** may stop
        for pi in range(n, -1, -1):
            later = later or rest[pi]
            if pi >= n and not empty:
                here[pi] = stars_from[j]
            elif pseg == "**":
                # Consume 0 or more path segments
                here[pi] = later
            elif empty:
                here[pi] = False
            else:
                # Single segment: fnmatch; ? and * don't cross /.
                here[pi] = rest[pi + 1] and fnmatch.fnmatch(path_segs[pi], pseg)
        rest = here
    return rest[0]


def is_path_included(rel_path: str, filters: list[PathFilter]) -> bool:
    """
    Return True if rel_path passes the include/exclude filter set.

    Rules:
    - If no include filters exist, all paths start as included.
    - If any include filters exist, path must match at least one to remain included.
    - Any exclude filter match removes the path from the set.
    - Matching is case-insensitive; ** matches across path segments.
    """
    rel_path_norm = rel_path.replace("\\", "/")
    includes = [f for f in filters if f.type == "include"]
    excludes = [f for f in filters if f.type == "exclude"]

    if includes:
        if not any(_glob_match(f.pattern, rel_path_norm) for f in includes):
            return False
    for ex in excludes:
        if _glob_match(ex.pattern, rel_path_norm):
            return False
    return True


def is_path_included_merged(
    rel_path: str,
    tenant_filters: list[PathFilter],
    library_filters: list[PathFilter],
) -> bool:
    """Evaluate path against merged tenant + library filters.

    Priority rules:
    1. Library exclude matches → BLOCKED (absolute, highest priority)
    2. Library include matches → ALLOWED (overrides tenant restrictions)
    3. Tenant exclude matches → BLOCKED
    4. Tenant includes exist but path doesn't match any → BLOCKED
    5. Default → ALLOWED
    """
    rel_path_norm = rel_path.replace("\\", "/")

    lib_includes = [f for f in library_filters if f.type == "include"]
    lib_excludes = [f for f in library_filters if f.type == "exclude"]
    tenant_includes = [f for f in tenant_filters if f.type == "include"]
    tenant_excludes = [f for f in tenant_filters if f.type == "exclude"]

    # Rule 1: Library exclude → blocked
    if any(_glob_match(f.pattern, rel_path_norm) for f in lib_excludes):
        return False

    # Rule 2: Library include → allowed (overrides tenant restrictions)
    if lib_includes and any(_glob_match(f.pattern, rel_path_norm) for f in lib_includes):
        return True

    # Rule 3: Tenant exclude → blocked
    if any(_glob_match(f.pattern, rel_path_norm) for f in tenant_excludes):
        return False

    # Rule 4: Tenant includes exist but path doesn't match → blocked
    if tenant_includes and not any(_glob_match(f.pattern, rel_path_norm) for f in tenant_includes):
        return False

    # Rule 5: Default → allowed
    return True


# A pattern's size: far past any real filter's (a folder a few deep, a name, an extension), as each is matched
# against every file a scan walks and every clip a filter's preview counts.
MAX_PATTERN_LENGTH = 1024
MAX_PATTERN_PARTS = 64


def validate_pattern(pattern: str) -> str:
    """
    Validate and return the pattern, or raise ValueError if invalid.
    Rejects patterns containing '..' or null bytes, longer than MAX_PATTERN_LENGTH
    characters, or of more than MAX_PATTERN_PARTS parts (between its slashes).
    """
    if "\x00" in pattern:
        raise ValueError("Pattern must not contain null bytes")
    if ".." in pattern:
        raise ValueError("Pattern must not contain '..'")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"Pattern must be at most {MAX_PATTERN_LENGTH} characters")
    if len(pattern.replace("\\", "/").split("/")) > MAX_PATTERN_PARTS:
        raise ValueError(f"Pattern must have at most {MAX_PATTERN_PARTS} parts between slashes")
    return pattern
