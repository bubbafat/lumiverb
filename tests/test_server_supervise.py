"""lumiverb-server run's supervisor (src/server/lifecycle/supervise.py): the
API and the scheduler as one service, as the brain's systemd units run each.
Real child processes: one that dies is started again, a stop sends SIGTERM
the way each unit does, and what's left is killed when its time is up."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from src.server.lifecycle.supervise import Child, Supervisor

pytestmark = pytest.mark.fast

# A child that records its start, and on SIGTERM records it and exits.
_POLITE = """
import os, signal, sys, time
log = sys.argv[1]
def note(what):
    with open(log, "a") as f:
        f.write(f"{what} {os.getpid()} {time.monotonic()}\\n")
note("start")
signal.signal(signal.SIGTERM, lambda *_: (note("term"), sys.exit(0)))
while True:
    time.sleep(0.05)
"""

# Dies at its first start; stays up at the next.
_DIES_ONCE = """
import os, sys, time
marker = sys.argv[1]
if not os.path.exists(marker):
    open(marker, "w").close()
    sys.exit(3)
with open(marker, "a") as f:
    f.write("again\\n")
while True:
    time.sleep(0.05)
"""

# Ignores SIGTERM: only a kill stops it.
_STUBBORN = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.05)
"""

# The scheduler with a job's ffmpeg under it: a grandchild that records any
# SIGTERM it gets and its pid.
_WITH_GRANDCHILD = """
import subprocess, sys, time, signal
grand = subprocess.Popen([sys.executable, "-c", '''
import os, signal, sys, time
log = sys.argv[1]
open(log + ".pid", "w").write(str(os.getpid()))
signal.signal(signal.SIGTERM, lambda *_: open(log + ".term", "w").close())
while True:
    time.sleep(0.05)
''', sys.argv[1]])
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(0.05)
"""


def _child(tmp_path: Path, name: str, code: str, *args: str, **kwargs) -> Child:
    script = tmp_path / f"{name}.py"
    script.write_text(code)
    return Child(name, [sys.executable, str(script), *args], dict(os.environ), tmp_path,
                 restart_after=kwargs.pop("restart_after", 0.1), stop_within=kwargs.pop("stop_within", 5), **kwargs)


def _running(supervisor: Supervisor):
    stop = threading.Event()
    thread = threading.Thread(target=supervisor.run, args=(stop,), kwargs={"poll_sec": 0.05})
    thread.start()
    return stop, thread


def _until(condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_child_that_dies_is_started_again_after_its_delay(tmp_path):
    marker = tmp_path / "died"
    child = _child(tmp_path, "api", _DIES_ONCE, str(marker), restart_after=0.3)
    stop, thread = _running(Supervisor([child]))
    _until(lambda: marker.exists() and "again" in marker.read_text())
    stop.set()
    thread.join(10)
    assert not thread.is_alive()


def test_a_stop_goes_in_reverse_order_and_each_child_hears_sigterm(tmp_path):
    api_log, scheduler_log = tmp_path / "api.log", tmp_path / "scheduler.log"
    api = _child(tmp_path, "api", _POLITE, str(api_log))
    scheduler = _child(tmp_path, "scheduler", _POLITE, str(scheduler_log), term_main_only=True)
    stop, thread = _running(Supervisor([api, scheduler]))
    _until(lambda: api_log.exists() and scheduler_log.exists())
    stop.set()
    thread.join(15)

    def term_time(log: Path) -> float:
        line = next(line for line in log.read_text().splitlines() if line.startswith("term"))
        return float(line.split()[2])

    assert term_time(scheduler_log) < term_time(api_log), "the scheduler stops before the API"


def test_only_the_schedulers_main_process_hears_the_stop_and_whats_left_is_killed(tmp_path):
    # KillMode=mixed: a job's ffmpeg isn't cut off by the stop, only killed
    # once the scheduler has gone.
    log = tmp_path / "grand"
    scheduler = _child(tmp_path, "scheduler", _WITH_GRANDCHILD, str(log), term_main_only=True)
    stop, thread = _running(Supervisor([scheduler]))
    _until(lambda: Path(f"{log}.pid").exists())
    grandchild = int(Path(f"{log}.pid").read_text())
    stop.set()
    thread.join(15)
    assert not Path(f"{log}.term").exists(), "the grandchild got no SIGTERM"
    _until(lambda: not _alive(grandchild), timeout=5)


def test_a_child_that_ignores_sigterm_is_killed_when_its_time_is_up(tmp_path):
    child = _child(tmp_path, "api", _STUBBORN, stop_within=0.5)
    stop, thread = _running(Supervisor([child]))
    _until(lambda: child.process is not None)
    pid = child.process.pid
    started = time.monotonic()
    stop.set()
    thread.join(10)
    assert not _alive(pid)
    assert 0.4 < time.monotonic() - started < 5


def test_its_temp_folder_is_emptied_before_it_starts(tmp_path):
    spool = tmp_path / "tmp"
    (spool / "left" / "over").mkdir(parents=True)
    (spool / "upload.part").write_text("from a killed API")
    log = tmp_path / "api.log"
    child = _child(tmp_path, "api", _POLITE, str(log), empty_first=spool)
    stop, thread = _running(Supervisor([child]))
    _until(log.exists)
    assert list(spool.iterdir()) == []
    stop.set()
    thread.join(10)
