"""Lumiverb's AI worker (ADR-022): CLIP and face detection for another
machine's scheduler, as an AI machine in its Settings › AI.

`lumiverb-server ai-worker` runs it (src/server/lifecycle/cli.py) from any
install: the same CLIP and InsightFace the scheduler loads (its Models, the
install's models folder), no account, database or files. app.py is its API;
here, its key: made once, kept in the install's data folder, owner-only, and
asked of every request.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

DEFAULT_PORT = 6366


def key_file(data: Path) -> Path:
    return data / "ai-worker" / "key"


def ensure_key(data: Path) -> str:
    """The worker's key: the one kept in data, or a new one made and kept
    (owner-only) when there's none."""
    path = key_file(data)
    try:
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    key = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (key + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return key
