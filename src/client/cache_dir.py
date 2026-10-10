"""Where Lumiverb keeps local caches: $XDG_CACHE_HOME/lumiverb, else the CLI
config's cache_home, else ~/.cache/lumiverb.

The brain's install sets both to its data disk (deploy-api.sh): the worker
service gets XDG_CACHE_HOME, a manual run as the lumiverb user the config.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def cache_dir(*parts: str) -> Path:
    from src.client.cli.config import load_config

    base = os.environ.get("XDG_CACHE_HOME") or load_config().cache_home or Path.home() / ".cache"
    return Path(base, "lumiverb", *parts)


_ASSET_ID = re.compile(r"[A-Za-z0-9_-]+")


def cache_entry(directory: Path, asset_id: str, suffix: str = "") -> Path:
    """directory/<asset_id><suffix>. Asset ids come from the server, so one
    that isn't a plain name (a path, "..") is refused rather than followed."""
    if not isinstance(asset_id, str) or not _ASSET_ID.fullmatch(asset_id):
        raise ValueError(f"Not an asset id: {asset_id!r}")
    return directory / f"{asset_id}{suffix}"
