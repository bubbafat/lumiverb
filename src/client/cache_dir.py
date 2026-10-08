"""Where Lumiverb keeps local caches: $XDG_CACHE_HOME/lumiverb, else ~/.cache/lumiverb.

The brain's worker sets XDG_CACHE_HOME to its data disk (deploy-api.sh).
"""

from __future__ import annotations

import os
from pathlib import Path


def cache_dir(*parts: str) -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base, "lumiverb", *parts)
