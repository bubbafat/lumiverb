"""Which Lumiverb is running, for the Admin page (GET /v1/system/health).

The package's own version (pyproject) isn't moved at a release, so it's
read from the install: a Homebrew keg is Cellar/lumiverb/<version>; a git
checkout (a Linux install's /opt/lumiverb, a development one) says what
`git describe` says; anything else is "unknown", never a guess.
"""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

# The install's top: the keg's libexec, or the checkout.
ROOT = Path(__file__).resolve().parents[2]


@functools.cache
def running() -> str:
    """"0.1.6" (Homebrew), "v0.1.6-3-g1a2b3c4" (a checkout past a tag), or "unknown"."""
    if found := re.search(r"/Cellar/lumiverb/([^/]+)/", f"{ROOT}/"):
        return found.group(1)
    if (ROOT / ".git").exists():
        try:
            result = subprocess.run(
                # A service user reads a checkout it doesn't own (Linux: root's /opt/lumiverb).
                ["git", "-c", "safe.directory=*", "-C", str(ROOT), "describe", "--tags", "--always", "--dirty"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return "unknown"
