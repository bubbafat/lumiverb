"""Where Lumiverb keeps local caches: $XDG_CACHE_HOME/lumiverb, else the CLI
config's cache_home, else ~/.cache/lumiverb.

The brain's install sets both to its data disk (deploy-api.sh): the worker
service gets XDG_CACHE_HOME, a manual run as the lumiverb user the config.
"""

from __future__ import annotations

import os
from pathlib import Path


def cache_dir(*parts: str) -> Path:
    from src.client.cli.config import load_config

    base = os.environ.get("XDG_CACHE_HOME") or load_config().cache_home or Path.home() / ".cache"
    return Path(base, "lumiverb", *parts)
