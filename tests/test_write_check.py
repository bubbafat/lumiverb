"""Whether Lumiverb can still write to a folder it was given (src/server/
write_check.py): a test write, again now and then after the folder was
chosen, with the reason said plainly; kept a while, re-tested sooner when it
failed, and never left to hang a request on a share that doesn't answer.
How it shows: the Admin page's disk and backups rows (here), Settings ›
Storage and Backups (tests/test_storage_api.py, tests/test_backups_api.py)."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.server import backups, write_check
from src.server import system_health as h
from src.server.storage import move

pytestmark = pytest.mark.fast

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
TB = 1024 ** 4


@pytest.fixture(autouse=True)
def _fresh():
    write_check._kept.clear()
    write_check._testing.clear()
    yield
    write_check._kept.clear()
    write_check._testing.clear()


def test_a_folder_it_can_write_says_nothing_and_leaves_nothing(tmp_path):
    assert write_check.try_write(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_one_it_cant_says_why(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        assert write_check.try_write(locked) == f"Lumiverb can't write to {locked}: no permission."
    finally:
        locked.chmod(0o700)
    gone = tmp_path / "gone"
    assert write_check.try_write(gone) == f"Lumiverb can't write to {gone}: it isn't there."


def test_the_reasons_read_plainly():
    import errno

    said = {code: write_check._reason(OSError(code, os.strerror(code)))
            for code in (errno.EROFS, errno.EACCES, errno.EPERM, errno.ENOSPC, errno.EIO)}
    assert said == {errno.EROFS: "it's read-only", errno.EACCES: "no permission", errno.EPERM: "no permission",
                    errno.ENOSPC: "the drive is full", errno.EIO: os.strerror(errno.EIO).lower()}


def test_an_answer_is_kept_a_while_and_a_failure_tested_again_sooner(tmp_path, monkeypatch):
    calls: list[Path] = []
    answers = iter(["Lumiverb can't write to x: it's read-only.", None, None])
    monkeypatch.setattr(write_check, "try_write", lambda folder: calls.append(folder) or next(answers))
    first = write_check.why_not(tmp_path, now=0.0)
    assert first == "Lumiverb can't write to x: it's read-only."
    assert write_check.why_not(tmp_path, now=write_check.FAILED_EVERY_SEC - 1) == first, "kept"
    assert write_check.why_not(tmp_path, now=write_check.FAILED_EVERY_SEC) is None, "fixed: seen soon"
    assert write_check.why_not(tmp_path, now=write_check.EVERY_SEC) is None, "kept longer when it could write"
    assert len(calls) == 2
    assert write_check.why_not(tmp_path, now=write_check.FAILED_EVERY_SEC + write_check.EVERY_SEC) is None
    assert len(calls) == 3


def test_a_folder_that_doesnt_answer_never_holds_the_request_nor_piles_up_tests(tmp_path, monkeypatch):
    release = threading.Event()
    calls: list[Path] = []

    def hangs(folder: Path) -> str | None:
        calls.append(folder)
        release.wait(60)
        return None

    monkeypatch.setattr(write_check, "try_write", hangs)
    stuck = f"Lumiverb can't write to {tmp_path}: it isn't answering."
    try:
        assert write_check.why_not(tmp_path, timeout=0.05) == stuck  # not waited out: release is 60 s off
        assert write_check.why_not(tmp_path, timeout=0.05) == stuck
        assert len(calls) == 1, "no second test while the first is stuck"
    finally:
        release.set()
    write_check._testing[str(tmp_path)].join(60)
    assert write_check.why_not(tmp_path) is None, "answered: tested again"
    assert len(calls) == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_choosing_a_folder_says_why_it_cant_be_written(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(move.MoveError, match=f"Lumiverb can't write to {locked}: no permission"):
            move.check_target(tmp_path / "data", locked)
        with pytest.raises(backups.BackupError, match=f"Lumiverb can't write to {locked}: no permission"):
            backups.check_folder(locked)
    finally:
        locked.chmod(0o700)


def test_the_disk_row_is_red_when_a_previews_folder_cant_be_written():
    why = "Lumiverb can't write to /Volumes/CACHE/previews: it's read-only."
    row = h.disk_row(free=TB, total=2 * TB, now=NOW, unwritable=[why])
    assert (row.state, row.reason, row.link) == (h.RED, why, "/settings/storage")
    # An unplugged drive is said first: writing there is moot.
    unplugged = h.disk_row(free=TB, total=2 * TB, now=NOW, unwritable=[why],
                           previews={"folder": "/Volumes/CACHE/previews", "available": False})
    assert "Is its drive plugged in?" in unplugged.reason
    assert h.disk_row(free=TB, total=2 * TB, now=NOW).state == h.GREEN


def test_the_backups_row_is_red_when_its_folder_cant_be_written():
    why = "Lumiverb can't write to /Volumes/Backup/Lumiverb: no permission."
    row = h.backups_row(newest=NOW - timedelta(hours=5), folder_error=why, now=NOW)
    assert (row.state, row.reason, row.link) == (h.RED, f"{why} The next backup can't be made.", "/settings/backups")
