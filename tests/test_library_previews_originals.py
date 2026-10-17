"""Whether a library's previews are on its originals' drive (Library settings
› Previews, routers/libraries.py): asked in the request, so never waited on.
A network share whose server sleeps (prod's CIFS mount of the Mac's drives)
can block a stat for minutes: the originals are reached as the scheduler
reaches a library's (src/processing/originals.py: the root's probe and the
drive's look each in a thread), for 2 seconds, and not knowing says nothing."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.processing import roots
from src.server.api.routers import libraries

pytestmark = pytest.mark.fast


@pytest.fixture(autouse=True)
def _every_folder_here(monkeypatch):
    monkeypatch.setattr(roots, "_configured_map", lambda: {"/": "/"})


def _library(root, library_id: str = "lib_a"):
    return SimpleNamespace(library_id=library_id, name="Trips", root_path=str(root))


def test_the_originals_drive_when_it_answers(tmp_path):
    assert libraries._originals_device(_library(tmp_path)) == os.stat(tmp_path).st_dev
    assert libraries._same_device(tmp_path, os.stat(tmp_path).st_dev) is True


def test_not_there_is_not_known(tmp_path):
    assert libraries._originals_device(_library(tmp_path / "gone")) is None
    assert libraries._same_device(tmp_path, None) is None


# A share that never answers in a test: far longer than any slow machine adds.
HANG_SEC = 30


def _named(prefix: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(prefix)]


def test_a_share_that_doesn_t_answer_is_never_waited_on(tmp_path, monkeypatch):
    """What's asserted is that the hang isn't waited for (30 s), and that no
    second probe starts while one is stuck: never how fast, which a busy
    machine can't promise."""
    def asleep(path, require_entries):
        time.sleep(HANG_SEC)
        return path

    monkeypatch.setattr(roots, "_probe", asleep)
    monkeypatch.setattr(libraries, "ORIGINALS_TIMEOUT_SEC", 0.2)
    library = _library(tmp_path, "lib_sleepy")
    started = time.monotonic()
    assert libraries._originals_device(library) is None
    assert time.monotonic() - started < HANG_SEC / 3, "the hang wasn't waited for"
    stuck = _named("root-probe-lib_sleepy")
    assert len(stuck) == 1
    # Asked again while that probe is still stuck: no second stuck thread.
    assert libraries._originals_device(library) is None
    assert _named("root-probe-lib_sleepy") == stuck


def test_a_stat_that_hangs_after_the_probe_isn_t_waited_on_either(tmp_path, monkeypatch):
    real_stat = os.stat
    target = str(tmp_path.resolve())  # worked out before stat is replaced: resolve() calls stat

    def slow_stat(path, *a, **k):
        if str(path) == target:
            time.sleep(HANG_SEC)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(roots, "_probe", lambda path, require_entries: Path(target))  # it answered
    monkeypatch.setattr(libraries, "ORIGINALS_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(os, "stat", slow_stat)
    started = time.monotonic()
    assert libraries._originals_device(_library(tmp_path, "lib_slow")) is None
    assert time.monotonic() - started < HANG_SEC / 3, "the hang wasn't waited for"
    stuck = _named("originals-stat-lib_slow")
    assert len(stuck) == 1
    # Asked again while that stat is still stuck: no second stuck thread.
    assert libraries._originals_device(_library(tmp_path, "lib_slow")) is None
    assert _named("originals-stat-lib_slow") == stuck
