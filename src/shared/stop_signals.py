"""Setting a threading.Event when a stop signal comes, without a deadlock.

A signal's Python handler runs in the main thread between any two of its
steps, including while that thread holds the lock Event.set() takes (inside
Event.wait(), which the main thread spends its time in). A handler that sets
the Event then waits on a lock its own thread holds, every later signal
piles another wait on top, and the process never stops. So the handler here
only writes a byte to a pipe; a thread of its own reads it and sets the
Event, taking the lock as any other thread would.
"""

from __future__ import annotations

import contextlib
import os
import signal
import threading
from collections.abc import Iterable


def set_on_signals(event: threading.Event, signals: Iterable[signal.Signals] = (signal.SIGTERM, signal.SIGINT)) -> None:
    """From now on each of `signals` sets `event` (soon after, from another
    thread). Call it from the main thread, as signal.signal must be."""
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)

    def handler(signum: int, frame: object) -> None:
        with contextlib.suppress(BlockingIOError):  # the pipe is full of stops already
            os.write(write_fd, b"\0")

    def setter() -> None:
        while os.read(read_fd, 1):
            event.set()

    threading.Thread(target=setter, name="stop-on-signal", daemon=True).start()
    for signum in signals:
        signal.signal(signum, handler)
