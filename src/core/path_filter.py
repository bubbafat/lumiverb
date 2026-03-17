"""Shared path filter evaluation for library ingest. Pure functions, no I/O."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Literal


@dataclass
class PathFilter:
    type: Literal["include", "exclude"]
    pattern: str


def _glob_match(pattern: str, path: str) -> bool:
    """
    Case-insensitive glob match supporting ** for cross-segment matching.
    Both pattern and path are lowercased before comparison.
    * and ? match within a single path segment; ** matches zero or more segments.
    """
    pl = pattern.lower().replace("\\", "/")
    path_l = path.lower().replace("\\", "/")
    # Preserve empty path as single empty segment for edge cases
    path_segs = path_l.split("/") if path_l else [""]
    pattern_segs = pl.split("/")

    def match_segment(pat: str, seg: str) -> bool:
        """Single segment: fnmatch; ? and * don't cross /."""
        return fnmatch.fnmatch(seg, pat)

    def match_from(pi: int, pseg_i: int) -> bool:
        """Match path_segs[pi:] against pattern_segs[pseg_i:]. ** consumes 0+ path segments."""
        if pseg_i >= len(pattern_segs):
            return pi >= len(path_segs) or (pi == 0 and path_segs == [""])
        if pi >= len(path_segs) and path_segs != [""]:
            return all(s == "**" for s in pattern_segs[pseg_i:])
        pseg = pattern_segs[pseg_i]
        if pseg == "**":
            # Consume 0 or more path segments
            max_skip = len(path_segs) - pi + 1
            for skip in range(max_skip):
                if match_from(pi + skip, pseg_i + 1):
                    return True
            return False
        if path_segs == [""]:
            return False
        if not match_segment(pseg, path_segs[pi]):
            return False
        return match_from(pi + 1, pseg_i + 1)

    return match_from(0, 0)


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


def validate_pattern(pattern: str) -> str:
    """
    Validate and return the pattern, or raise ValueError if invalid.
    Rejects patterns containing '..' or null bytes.
    """
    if "\x00" in pattern:
        raise ValueError("Pattern must not contain null bytes")
    if ".." in pattern:
        raise ValueError("Pattern must not contain '..'")
    return pattern
