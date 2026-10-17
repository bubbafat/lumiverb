"""Where the scheduler keeps its caches, lock and state: $XDG_CACHE_HOME/
lumiverb, else ~/.cache/lumiverb. An install sets XDG_CACHE_HOME to its data
disk (deploy-api.sh; a Mac's env file). A client keeps none of the
scheduler's: the CLI caches nothing (ADR-020 decision 7).
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def cache_dir(*parts: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base, "lumiverb", *parts)


_ASSET_ID = re.compile(r"[A-Za-z0-9_-]+")


def cache_entry(directory: Path, asset_id: str, suffix: str = "") -> Path:
    """directory/<asset_id><suffix>. Asset ids come from the server, so one
    that isn't a plain name (a path, "..") is refused rather than followed."""
    if not isinstance(asset_id, str) or not _ASSET_ID.fullmatch(asset_id):
        raise ValueError(f"Not an asset id: {asset_id!r}")
    return directory / f"{asset_id}{suffix}"
