"""The plug-in runner (ADR-023 phase 1b) with real plug-in processes: the
test plug-in (tests/plugins/hello/) going wrong each way a plug-in might.
Whatever one does, the runner goes on, the others keep running, and what
happened is said, a reason each: a crash is started again, waiting longer
each time, and left stopped when it keeps crashing; one that hangs or
answers nonsense is ended and started again; too much memory is ended; a
file bigger than it may write fails its write; a data folder over its size
is ended and left stopped. It gets none of Lumiverb's settings, runs at a
lower priority, its output is said in the log by name, a new install is
started afresh and an uninstalled one stopped."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.plugins.manifest import MANIFEST, parse
from src.server import plugin_runner
from src.server.plugin_install import INSTALL_MARK, program_dir
from src.server.plugin_runner import (
    FAILING,
    PREPARING,
    READY,
    RESTARTING,
    STOPPED,
    Runner,
    read_state,
    socket_path,
)

pytestmark = pytest.mark.fast
HELLO = Path(__file__).parent / "plugins" / "hello"


@pytest.fixture
def data(monkeypatch) -> Iterator[Path]:
    """A data folder short enough for a Unix socket's path (macOS: 104 bytes), the runner's waits shortened,
    and python3 on the PATH being this Python."""
    root = Path(tempfile.mkdtemp(prefix="lvp", dir="/tmp"))
    bin_dir = root / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for name, value in (("START_WITHIN_SEC", 3.0), ("HEALTH_EVERY_SEC", 0.3), ("HEALTH_TIMEOUT_SEC", 0.5),
                        ("MEMORY_EVERY_SEC", 0.3), ("DISK_EVERY_SEC", 0.3), ("STOP_WITHIN_SEC", 2.0),
                        ("RESTART_FIRST_SEC", 0.2), ("RESTART_MAX_SEC", 0.5), ("GIVE_UP_AFTER", 3),
                        ("LIST_EVERY_SEC", 0.2), ("HEARTBEAT_SEC", 0.2)):
        monkeypatch.setattr(plugin_runner, name, value)
    yield root / "data"
    shutil.rmtree(root, ignore_errors=True)


class Installs:
    """What's installed, as the runner reads it: each a copy of the test plug-in in its program folder."""

    def __init__(self, data: Path) -> None:
        self.data, self.now = data, {}

    def add(self, plugin_id: str, mode: str = "", **change) -> None:
        program = program_dir(self.data, plugin_id)
        if program.exists():
            shutil.rmtree(program)
        shutil.copytree(HELLO, program)
        (program / INSTALL_MARK).write_text(uuid.uuid4().hex)  # as an install marks it
        manifest = json.loads((HELLO / MANIFEST).read_text())
        manifest.update(id=plugin_id, run=["python3", "hello.py", *([mode] if mode else [])], **change)
        self.now[plugin_id] = SimpleNamespace(manifest=parse(manifest))

    def __call__(self) -> dict:
        return dict(self.now)


def _runner(data: Path) -> tuple[Runner, Installs]:
    installs = Installs(data)
    return Runner(data, installed=installs), installs


def _until(runner: Runner, done: Callable[[], bool], within: float = 15.0) -> None:
    end = time.monotonic() + within
    while time.monotonic() < end:
        runner.turn()
        if done():
            return
        time.sleep(0.05)
    raise AssertionError(f"not within {within} s: {[p.said() for p in runner.plugins.values()]}")


def _state(runner: Runner, plugin_id: str) -> str:
    return runner.plugins[plugin_id].state if plugin_id in runner.plugins else "absent"


def test_a_plug_in_starts_answers_and_is_said_ready_by_name(data, caplog) -> None:
    caplog.set_level(logging.INFO, logger=plugin_runner.__name__)
    runner, installs = _runner(data)
    installs.add("hello")
    try:
        _until(runner, lambda: _state(runner, "hello") == READY)
        said = read_state(data)
        assert said["hello"]["state"] == "ready" and said["hello"]["version"] == "1"
        _until(runner, lambda: any("plugin hello | hello ready" in r.getMessage() for r in caplog.records))
        assert socket_path(data, "hello").exists()
    finally:
        runner.stop_all()
    assert read_state(data)["hello"]["state"] == "stopped"


def test_it_gets_none_of_lumiverbs_settings_and_runs_below_lumiverb(data, monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_DATABASE_URL", "postgresql://u:secret@localhost/control")
    monkeypatch.setenv("JWT_SECRET", "not-for-plug-ins")
    monkeypatch.setenv("DATA_DIR", str(data))
    runner, installs = _runner(data)
    installs.add("hello", "env")
    try:
        _until(runner, lambda: _state(runner, "hello") == READY)
        given = json.loads((data / "plugins" / "hello" / "data" / "env.json").read_text())
        assert not {"CONTROL_PLANE_DATABASE_URL", "JWT_SECRET", "DATA_DIR"} & set(given)
        assert given["LUMIVERB_PLUGIN_SOCKET"] == str(socket_path(data, "hello"))
        assert given["LUMIVERB_PLUGIN_DATA"] == str(data / "plugins" / "hello" / "data")
        assert given["HOME"] == given["LUMIVERB_PLUGIN_DATA"] and given["LUMIVERB_PLUGIN_ID"] == "hello"
        pid = runner.plugins["hello"].process.pid
        assert os.getpriority(os.PRIO_PROCESS, pid) >= os.getpriority(os.PRIO_PROCESS, 0) + 10
    finally:
        runner.stop_all()


def test_one_that_keeps_crashing_is_started_again_then_left_stopped_and_the_rest_go_on(data) -> None:
    runner, installs = _runner(data)
    installs.add("hello")
    try:
        _until(runner, lambda: _state(runner, "hello") == READY)
        installs.add("crasher", "crash")
        _until(runner, lambda: _state(runner, "crasher") == RESTARTING)
        assert runner.plugins["crasher"].why == "It stopped (exit code 3): crashing on purpose"
        _until(runner, lambda: _state(runner, "crasher") == STOPPED)
        crasher = runner.plugins["crasher"]
        assert crasher.why.startswith("It stopped (exit code 3): crashing on purpose. It stopped 3 times in a row")
        assert crasher.restarts == 2 and crasher.process is None
        assert _state(runner, "hello") == READY
        time.sleep(1)
        runner.turn()
        assert _state(runner, "crasher") == STOPPED, "left stopped"
        # Installed again: tried again.
        installs.add("crasher")
        _until(runner, lambda: _state(runner, "crasher") == READY)
    finally:
        runner.stop_all()


@pytest.mark.parametrize(("mode", "why"), [
    ("hang", "It didn't answer within 3 s of starting"),
    ("memory", "MB of memory, more than its 128 MB"),
])
def test_one_that_hangs_or_takes_too_much_memory_is_ended_and_started_again(data, mode, why) -> None:
    runner, installs = _runner(data)
    installs.add("hello")
    try:
        _until(runner, lambda: _state(runner, "hello") == READY)
        installs.add("trouble", mode, memory_mb=128)
        _until(runner, lambda: _state(runner, "trouble") == RESTARTING, within=20)
        assert why in runner.plugins["trouble"].why
        assert _state(runner, "hello") == READY
    finally:
        runner.stop_all()


def test_one_that_stops_answering_or_answers_nonsense_is_ended(data) -> None:
    runner, installs = _runner(data)
    installs.add("trouble", "garbage")
    try:
        _until(runner, lambda: _state(runner, "trouble") in (RESTARTING, STOPPED), within=20)
        assert "It didn't answer within 3 s of starting" in runner.plugins["trouble"].why
    finally:
        runner.stop_all()


def test_preparing_and_failing_are_said_as_it_says_them(data) -> None:
    runner, installs = _runner(data)
    installs.add("slow", "preparing")
    installs.add("broken", "failing")
    try:
        _until(runner, lambda: _state(runner, "slow") == PREPARING and _state(runner, "broken") == FAILING)
        slow = read_state(data)["slow"]
        assert (slow["done"], slow["total"], slow["detail"]) == (3, 10, "Reading the list")
        assert read_state(data)["broken"]["why"] == "Its list is broken"
        assert runner.plugins["broken"].process.poll() is None, "it says it can't work: still asked"
    finally:
        runner.stop_all()


def test_a_file_bigger_than_it_may_write_fails_its_write_and_a_full_folder_stops_it(data, caplog) -> None:
    caplog.set_level(logging.INFO, logger=plugin_runner.__name__)
    runner, installs = _runner(data)
    installs.add("writer", "big-file")
    installs.add("filler", "fill")
    try:
        _until(runner, lambda: any("plugin writer | couldn't write it: File too large" in r.getMessage()
                                   for r in caplog.records))
        assert (data / "plugins" / "writer" / "data" / "big").stat().st_size <= 10 * 1024 * 1024
        _until(runner, lambda: _state(runner, "writer") == READY)  # the write failed; it goes on
        _until(runner, lambda: _state(runner, "filler") == STOPPED, within=20)
        assert "more than its 10 MB: left stopped" in runner.plugins["filler"].why
    finally:
        runner.stop_all()


def test_a_new_install_starts_afresh_and_an_uninstalled_one_stops(data) -> None:
    runner, installs = _runner(data)
    installs.add("hello")
    try:
        _until(runner, lambda: _state(runner, "hello") == READY)
        first = runner.plugins["hello"].process
        installs.add("hello", version="2")
        _until(runner, lambda: runner.plugins["hello"].manifest.version == "2"
               and _state(runner, "hello") == READY)
        assert first.poll() is not None and runner.plugins["hello"].process is not first
        del installs.now["hello"]
        _until(runner, lambda: "hello" not in runner.plugins)
        assert runner.plugins == {}
        time.sleep(0.3)
        assert first.poll() is not None
    finally:
        runner.stop_all()


def test_what_the_runner_says_goes_stale_when_it_stops_saying_it(data) -> None:
    assert read_state(data) is None
    runner, installs = _runner(data)
    installs.add("hello")
    try:
        _until(runner, lambda: _state(runner, "hello") == READY)
        assert read_state(data, fresh_within=60) is not None
        time.sleep(1.1)
        assert read_state(data, fresh_within=1) is None, "not said for a while: the runner isn't running"
    finally:
        runner.stop_all()
