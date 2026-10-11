"""Whether Lumiverb can still write to a folder it was given: the previews',
a library's own, the backups'. Each gets a test write when it's chosen
(move.check_target, backups.check_folder), but a drive can stop taking
writes afterwards (remounted read-only, reformatted, its permissions
changed), and nothing said so until uploads or the nightly backup failed.
The Admin page's rows and Settings › Storage and Backups test again: a
folder that took the write at most every EVERY_SEC, one that didn't every
FAILED_EVERY_SEC (so a fix shows soon), in a thread given TIMEOUT_SEC,
since a network share whose server sleeps can block a write for minutes."""

from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path

EVERY_SEC = 300.0
FAILED_EVERY_SEC = 30.0
TIMEOUT_SEC = 2.0

_lock = threading.Lock()
# Per folder: when it was last tested (monotonic) and what it said.
_kept: dict[str, tuple[float, str | None]] = {}
# Per folder: the test running now. While one is stuck, no other starts.
_testing: dict[str, threading.Thread] = {}


def _reason(exc: OSError) -> str:
    if exc.errno == errno.EROFS:
        return "it's read-only"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return "no permission"
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return "the drive is full"
    if exc.errno == errno.ENOENT:
        return "it isn't there"
    return (exc.strerror or str(exc)).lower()


def try_write(folder: Path) -> str | None:
    """Why Lumiverb can't write to `folder`, said for the user; None when it
    can: a file written there and deleted. Here and now: it can hang."""
    probe = folder / f".lumiverb-write-check-{os.getpid()}-{threading.get_ident()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        return f"Lumiverb can't write to {folder}: {_reason(exc)}."
    return None


def why_not(folder: Path, *, now: float | None = None, every: float = EVERY_SEC,
            failed_every: float = FAILED_EVERY_SEC, timeout: float = TIMEOUT_SEC) -> str | None:
    """try_write's answer for `folder`, kept `every` seconds (`failed_every`
    when it couldn't write); one that doesn't answer within `timeout`
    seconds says so, and no other test of it starts until it does."""
    key = str(folder)
    now = time.monotonic() if now is None else now
    stuck = f"Lumiverb can't write to {folder}: it isn't answering."
    answer: dict[str, str | None] = {}

    def run() -> None:
        answer["why"] = try_write(folder)

    with _lock:
        kept = _kept.get(key)
        if kept is not None and now - kept[0] < (every if kept[1] is None else failed_every):
            return kept[1]
        running = _testing.get(key)
        if running is not None and running.is_alive():
            return stuck
        thread = threading.Thread(target=run, name="write-check", daemon=True)
        _testing[key] = thread
    thread.start()
    thread.join(timeout)
    if "why" not in answer:
        return stuck
    with _lock:
        _kept[key] = (now, answer["why"])
    return answer["why"]
