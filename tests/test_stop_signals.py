"""A stop signal sets the stop Event without a deadlock (src/shared/stop_signals.py).

lumiverb-server run and the scheduler wait on a threading.Event in their main
thread and stop when SIGTERM sets it. A handler that called Event.set() itself
could run while the main thread held the Event's own lock (inside wait()) and
wait on it forever: the service never stopped, and every later signal piled
on. Under a stream of SIGTERMs that happened within a second or two.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

REPO = Path(__file__).resolve().parents[1]

# The main thread spends its time in Event.wait(), as run's and the scheduler's do.
BUSY = """
import sys, threading, time
from src.shared.stop_signals import set_on_signals
stop = threading.Event()
set_on_signals(stop)
done = [0]
def report():
    while True:
        print(done[0], flush=True); time.sleep(0.1)
threading.Thread(target=report, daemon=True).start()
while True:
    if stop.wait(0.0001):
        stop.clear()
    done[0] += 1
"""

STOPS = """
import threading
from src.shared.stop_signals import set_on_signals
stop = threading.Event()
set_on_signals(stop)
print("waiting", flush=True)
while not stop.wait(0.0001):
    pass
print("stopped", flush=True)
"""


def _start(code: str) -> tuple[subprocess.Popen, list[str]]:
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=REPO, stdout=subprocess.PIPE, text=True)
    said: list[str] = []
    threading.Thread(target=lambda: said.extend(line.strip() for line in proc.stdout), daemon=True).start()
    deadline = time.monotonic() + 30
    while not said:  # its handler is in place once it speaks
        assert proc.poll() is None and time.monotonic() < deadline, "it never started"
        time.sleep(0.01)
    return proc, said


def test_a_stream_of_stop_signals_never_stops_the_main_thread_for_good():
    proc, said = _start(BUSY)
    try:
        end = time.monotonic() + 2
        while time.monotonic() < end:
            os.kill(proc.pid, signal.SIGTERM)
            time.sleep(0.0002)
        assert proc.poll() is None, "a signal killed it: the handler wasn't in place"
        time.sleep(0.5)
        before = int(said[-1])
        time.sleep(0.5)
        assert int(said[-1]) > before, "the main thread stopped making progress"
    finally:
        proc.kill()
        proc.wait()


def test_a_stop_signal_sets_the_event():
    proc, said = _start(STOPS)
    try:
        os.kill(proc.pid, signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        time.sleep(0.1)
        assert said[-1] == "stopped"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_every_stop_signal_goes_through_it():
    handlers = [f"{path.relative_to(REPO)}:{n}" for path in (REPO / "src").rglob("*.py")
                if path.name != "stop_signals.py"
                for n, line in enumerate(path.read_text(errors="ignore").splitlines(), 1) if "signal.signal(" in line]
    assert handlers == []
