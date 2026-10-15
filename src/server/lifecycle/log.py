"""A Mac's service log (lumiverb-server run): readable, and of a bounded size.

launchd writes the service's stdout and stderr to one file (brew services'
log_path, /opt/homebrew/var/log/lumiverb.log), appended to forever: on a
busy evening 10 MB in two and a half hours, most of it a line per web
request and Quickwit's coloured chatter (Quickwit is gone: ADR-019). Here
each child's output comes through run (relay): each line said by whom ("api | ...", "nginx | ..."),
its colour codes out, into that same file, which is rotated at LOG_BYTES,
LOG_KEPT kept beside it. The process's own stdout and stderr follow the
current file (a stray print, a crash's traceback). Not launched by launchd
(a terminal, a pipe), lines go to stderr as before, named the same.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import stat
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO

LOG_BYTES = 10 * 1024 * 1024
LOG_KEPT = 4  # lumiverb.log.1 ... .4 beside the current: at most 50 MB in all
RELAYED = "lumiverb-server.relay"
QUIET = ("alembic.runtime.plugins",)  # six "setup plugin ..." lines every start
_COLOUR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def file_of(fd: int) -> Path | None:
    """The regular file `fd` writes to (launchd's log), else None."""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
    except OSError:
        return None
    with contextlib.suppress(OSError, AttributeError):
        import fcntl

        named = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))  # macOS
        return Path(named.split(b"\0", 1)[0].decode())
    with contextlib.suppress(OSError):
        return Path(os.readlink(f"/proc/self/fd/{fd}"))  # Linux
    return None


class _Rotating(RotatingFileHandler):
    """Rotates, and the process's own stdout and stderr follow the new file:
    launchd's fds would write on to the old one, deleted in its turn."""

    def doRollover(self) -> None:  # noqa: N802 — logging's own name
        super().doRollover()
        if self.stream is not None:
            for fd in (1, 2):
                with contextlib.suppress(OSError):
                    os.dup2(self.stream.fileno(), fd)


class _Said(logging.Formatter):
    """A child's line as relayed ("api | ..."); run's own, "lumiverb-server: ..."."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return text if record.name == RELAYED else f"lumiverb-server: {text}"


def configure(stderr_fd: int = 2) -> logging.Handler:
    """run's logging: to launchd's file, rotated, when stderr is one; else
    stderr. Once per process (run again in it, the same handler)."""
    root = logging.getLogger()
    for existing in root.handlers:
        if isinstance(existing.formatter, _Said):
            return existing
    path = file_of(stderr_fd)
    handler: logging.Handler = (_Rotating(path, maxBytes=LOG_BYTES, backupCount=LOG_KEPT, encoding="utf-8")
                                if path else logging.StreamHandler())
    handler.setFormatter(_Said("%(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def relay(name: str, stream: IO[bytes]) -> None:
    """A child's output, line by line, said by whom and without colour
    codes, until it closes. Runs in a thread of its own per child."""
    log = logging.getLogger(RELAYED)
    try:
        for raw in iter(stream.readline, b""):
            line = _COLOUR.sub("", raw.decode("utf-8", "replace")).rstrip("\r\n")
            if line:
                log.info("%s | %s", name, line)
    finally:
        with contextlib.suppress(OSError):
            stream.close()
