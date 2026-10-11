"""The API and the scheduler as one service: what the brain's systemd units
(deploy-api.sh) do for each, for a Mac, where launchd runs one process per
Homebrew service.

A child can wait for something first (`start_after`): the API and the
scheduler wait for migrations, which wait for the PostgreSQL started here.

Each child starts in its own process group. One that dies is started again
after its delay. systemd restarts only on failure, but here nothing else
would, so any exit is restarted. One that keeps exiting soon after it starts
(a bad setting, a missing model) waits twice as long each time, up to 10
minutes, where systemd's start limit would give up. On stop, children stop
in reverse order (the scheduler before the API), each sent SIGTERM the way
its unit has it, then killed with what's left once its time is up.
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

RESTART_MAX_SEC = 600.0
# Up this long, a child that exits had been working: its next start waits only its own delay.
STAYED_UP_SEC = 180.0


@dataclass
class Child:
    name: str
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    restart_after: float  # seconds, as the unit's RestartSec
    stop_within: float  # seconds after the stop signal before what's left is killed, as TimeoutStopSec
    # SIGTERM to the main process only (KillMode=mixed: the scheduler lets
    # jobs in hand finish, and ffmpeg under one isn't cut off mid-clip), or to
    # the whole group (uvicorn and its workers).
    term_main_only: bool = False
    stop_signal: int = signal.SIGTERM  # PostgreSQL's fast shutdown is SIGINT
    empty_first: Path | None = None  # emptied before each start, as its ExecStartPre
    start_after: threading.Event | None = field(default=None, repr=False)  # first start once it's set
    process: subprocess.Popen | None = field(default=None, repr=False)
    started: bool = False
    started_at: float | None = None
    restart_at: float | None = None
    delay: float | None = None  # its last wait before a start, while it keeps exiting


def restart_delay(child: Child, uptime: float) -> float:
    """How long before a child that exited is started again: its unit's
    RestartSec, doubled for each exit soon after a start, up to RESTART_MAX_SEC."""
    if child.delay is None or uptime >= STAYED_UP_SEC:
        return child.restart_after
    return min(child.delay * 2, max(RESTART_MAX_SEC, child.restart_after))


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
        child.started = True
        child.started_at = self._clock()
        child.restart_at = None
        logger.info("%s started (pid %d)", child.name, child.process.pid)

    def _watch(self, child: Child) -> None:
        if not child.started:
            if child.start_after is not None and child.start_after.is_set():
                self._start(child)
            return
        now = self._clock()
        if child.process is not None and child.process.poll() is not None:
            uptime = now - (child.started_at or now)
            child.delay = restart_delay(child, uptime)
            logger.warning("%s exited (%s) after %.0f s; starting it again in %.0f s",
                           child.name, child.process.returncode, uptime, child.delay)
            _signal_group(child.process, signal.SIGKILL)  # anything it left behind
            child.process = None
            child.restart_at = now + child.delay
        if child.process is None and child.restart_at is not None and now >= child.restart_at:
            self._start(child)

    def _stop(self, child: Child) -> None:
        process = child.process
        if process is None or process.poll() is not None:
            return
        if child.term_main_only:
            process.send_signal(child.stop_signal)
        else:
            _signal_group(process, child.stop_signal)
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
            if child.start_after is None:
                self._start(child)
        try:
            while not stop.wait(poll_sec):
                for child in self.children:
                    self._watch(child)
        finally:
            for child in reversed(self.children):
                self._stop(child)
