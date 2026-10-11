"""Whether a library's previews are on its originals' drive (Library settings
› Previews, routers/libraries.py): asked in the request, so never waited on.
A network share whose server sleeps (prod's CIFS mount of the Mac's drives)
can block a stat for minutes: the originals are probed as the scheduler
probes a library's root (roots.reachable_root, in a thread), for 2 seconds,
and not knowing says nothing."""

from __future__ import annotations

import os
import threading
import time
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


def test_a_share_that_doesn_t_answer_is_never_waited_on(tmp_path, monkeypatch):
    def asleep(path, require_entries):
        time.sleep(3)
        return path

    monkeypatch.setattr(roots, "_probe", asleep)
    monkeypatch.setattr(libraries, "ORIGINALS_TIMEOUT_SEC", 0.2)
    library = _library(tmp_path, "lib_sleepy")
    started = time.monotonic()
    assert libraries._originals_device(library) is None
    assert time.monotonic() - started < 1.0
    # Asked again while that probe is still stuck: no wait, and no second stuck thread.
    started = time.monotonic()
    assert libraries._originals_device(library) is None
    assert time.monotonic() - started < 0.1


def test_a_stat_that_hangs_after_the_probe_isn_t_waited_on_either(tmp_path, monkeypatch):
    real_stat = os.stat

    def slow_stat(path, *a, **k):
        if str(path) == str(tmp_path.resolve()):
            time.sleep(3)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(roots, "_probe", lambda path, require_entries: path.resolve())  # it answered
    monkeypatch.setattr(libraries, "ORIGINALS_TIMEOUT_SEC", 0.2)
    monkeypatch.setattr(os, "stat", slow_stat)
    started = time.monotonic()
    assert libraries._originals_device(_library(tmp_path, "lib_slow")) is None
    assert time.monotonic() - started < 1.0
    # Asked again while that stat is still stuck: no wait, and no second stuck thread.
    stuck = [t for t in threading.enumerate() if t.name == "originals-stat-lib_slow"]
    started = time.monotonic()
    assert libraries._originals_device(_library(tmp_path, "lib_slow")) is None
    assert time.monotonic() - started < 0.5
    assert [t for t in threading.enumerate() if t.name == "originals-stat-lib_slow"] == stuck
