"""Where Lumiverb keeps local caches: $XDG_CACHE_HOME/lumiverb, else this
machine's cache_home (src/processing/machine.py), else ~/.cache/lumiverb.

The brain's install sets XDG_CACHE_HOME to its data disk for the scheduler
(deploy-api.sh), and the lumiverb user's CLI config the same, so a command
run by hand finds the scheduler's caches and lock.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def cache_dir(*parts: str) -> Path:
    from src.processing.machine import current

    base = os.environ.get("XDG_CACHE_HOME") or current().cache_home or Path.home() / ".cache"
    return Path(base, "lumiverb", *parts)


_ASSET_ID = re.compile(r"[A-Za-z0-9_-]+")


def cache_entry(directory: Path, asset_id: str, suffix: str = "") -> Path:
    """directory/<asset_id><suffix>. Asset ids come from the server, so one
    that isn't a plain name (a path, "..") is refused rather than followed."""
    if not isinstance(asset_id, str) or not _ASSET_ID.fullmatch(asset_id):
        raise ValueError(f"Not an asset id: {asset_id!r}")
    return directory / f"{asset_id}{suffix}"
