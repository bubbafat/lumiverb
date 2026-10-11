"""The API and the scheduler as one service: what the brain's systemd units
(deploy-api.sh) do for each, for a Mac, where launchd runs one process per
Homebrew service.

Each child starts in its own process group. One that dies is started again
after its delay. systemd restarts only on failure, but here nothing else
would, so any exit is restarted. On stop, children stop in reverse order
(the scheduler before the API), each sent SIGTERM the way its unit has it,
then killed with what's left once its time is up.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Child:
    name: str
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    restart_after: float  # seconds, as the unit's RestartSec
    stop_within: float  # seconds after SIGTERM before what's left is killed, as TimeoutStopSec
    # SIGTERM to the main process only (KillMode=mixed: the scheduler lets
    # jobs in hand finish, and ffmpeg under one isn't cut off mid-clip), or to
    # the whole group (uvicorn and its workers).
    term_main_only: bool = False
    empty_first: Path | None = None  # emptied before each start, as its ExecStartPre
    process: subprocess.Popen | None = field(default=None, repr=False)
    restart_at: float | None = None


def _empty(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for entry in directory.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, sig)


class Supervisor:
    def __init__(self, children: list[Child], *, clock: Callable[[], float] = time.monotonic,
                 spawn: Callable[..., subprocess.Popen] = subprocess.Popen) -> None:
        self.children = children
        self._clock = clock
        self._spawn = spawn

    def _start(self, child: Child) -> None:
        if child.empty_first is not None:
            _empty(child.empty_first)
        child.process = self._spawn(child.argv, env=child.env, cwd=child.cwd, start_new_session=True)
        child.restart_at = None
        logger.info("%s started (pid %d)", child.name, child.process.pid)

    def _watch(self, child: Child) -> None:
        now = self._clock()
        if child.process is not None and child.process.poll() is not None:
            logger.warning("%s exited (%s); starting it again in %.0f s",
                           child.name, child.process.returncode, child.restart_after)
            _signal_group(child.process, signal.SIGKILL)  # anything it left behind
            child.process = None
            child.restart_at = now + child.restart_after
        if child.process is None and child.restart_at is not None and now >= child.restart_at:
            self._start(child)

    def _stop(self, child: Child) -> None:
        process = child.process
        if process is None or process.poll() is not None:
            return
        if child.term_main_only:
            process.send_signal(signal.SIGTERM)
        else:
            _signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=child.stop_within)
        except subprocess.TimeoutExpired:
            logger.warning("%s didn't stop within %.0f s: killed", child.name, child.stop_within)
        _signal_group(process, signal.SIGKILL)  # what's left, as at the end of a unit's stop
        process.wait()
        logger.info("%s stopped", child.name)

    def run(self, stop: threading.Event, poll_sec: float = 0.5) -> None:
        """Starts the children and keeps them up until `stop` is set, then stops them."""
        for child in self.children:
            self._start(child)
        try:
            while not stop.wait(poll_sec):
                for child in self.children:
                    self._watch(child)
        finally:
            for child in reversed(self.children):
                self._stop(child)
