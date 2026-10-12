"""Where the previews are, and moving them (src/shared/previews_location.py,
src/server/storage/local.py, src/server/storage/move.py): read where they
are during a move, written to the new folder, deleted in both; the mover
carries every file across without writing over one made meanwhile, stops
on an unplugged or full drive and goes on after; a drive that isn't there
is never written to; cleanup and repairs wait for a move; a library walk
never goes into the data or previews folder. On real folders, no database."""

from __future__ import annotations

import errno
import json
import os
import shutil
import time
from collections import namedtuple
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.server import system_health as h
from src.server.storage import move
from src.server.storage.local import LocalStorage, UnsafeKeyError
from src.shared import previews_location

pytestmark = pytest.mark.fast

PROXY = "ten_a/lib_1/proxies/07/ast_1_photo.webp"
THUMB = "ten_a/lib_1/thumbnails/07/ast_1_photo.webp"
ANALYSIS = "ten_a/lib_2/analysis/03/ast_2_clip.mp4"
CUT = "ten_a/playback/ast_2_analysis_proxy_abc_10s.mp4"


@pytest.fixture
def install(tmp_path):
    """A data folder with an install's own things and some previews; a drive to move them to."""
    data = tmp_path / "data"
    for own in ("postgres/base", "quickwit", "cache/lumiverb/analysis"):
        (data / own).mkdir(parents=True)
    (data / "postgres/PG_VERSION").write_text("18")
    for key, body in ((PROXY, b"proxy"), (THUMB, b"thumb"), (ANALYSIS, b"analysis" * 100), (CUT, b"cut")):
        path = data / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    drive = tmp_path / "Volumes" / "Media"
    drive.mkdir(parents=True)
    return data, drive


def _moving(data: Path, drive: Path) -> None:
    """A move recorded as begun, nothing carried yet."""
    (drive / previews_location.MARKER).write_text("")
    previews_location.write(data, drive, data)


# -- Where -----------------------------------------------------------------------


def test_until_one_is_chosen_they_re_in_the_data_folder(install):
    data, _ = install
    storage = LocalStorage(str(data))
    assert storage.root == data and storage.location.roots == (data,)
    assert storage.abs_path(PROXY) == data / PROXY and storage.write_path(PROXY) == data / PROXY
    assert previews_location.read(data) == previews_location.Location(data)


def test_during_a_move_read_where_it_is_write_to_the_new_folder_delete_in_both(install):
    data, drive = install
    storage = LocalStorage(str(data))
    _moving(data, drive)
    assert storage.root == drive  # seen at once: storage.json changed
    assert storage.abs_path(PROXY) == data / PROXY, "not moved yet: read where it is"
    assert storage.write_path(PROXY) == drive / PROXY
    storage.write(THUMB, b"new thumb")
    assert storage.abs_path(THUMB) == drive / THUMB, "the new one is read"
    assert storage.exists(PROXY) and storage.exists(THUMB)
    assert storage.abs_path("ten_a/lib_9/proxies/00/none.webp") == drive / "ten_a/lib_9/proxies/00/none.webp"
    assert storage.delete(THUMB)
    assert not (drive / THUMB).exists() and not (data / THUMB).exists()
    assert not storage.delete(THUMB)


def test_a_key_never_leaves_either_folder(install):
    data, drive = install
    _moving(data, drive)
    storage = LocalStorage(str(data))
    for bad in ("../etc/passwd", "/etc/passwd", "ten_a/../../x", ""):
        with pytest.raises(UnsafeKeyError):
            storage.abs_path(bad)
        with pytest.raises(UnsafeKeyError):
            storage.delete(bad)


def test_nothing_is_written_to_a_chosen_folder_whose_drive_is_unplugged(install):
    """Its empty mount point is on the system disk."""
    data, drive = install
    _moving(data, drive)
    (drive / previews_location.MARKER).unlink()
    storage = LocalStorage(str(data))
    with pytest.raises(FileNotFoundError, match="Is its drive plugged in"):
        storage.write(PROXY, b"x")
    assert not (drive / PROXY).exists()
    assert storage.abs_path(PROXY) == data / PROXY, "reading what's there still works"


# -- Moving ------------------------------------------------------------------------


def test_a_move_carries_every_preview_and_leaves_the_install_s_own_things(install):
    data, drive = install
    move.start(data, drive, background=False)
    assert previews_location.read(data) == previews_location.Location(drive)
    assert (drive / PROXY).read_bytes() == b"proxy" and (drive / ANALYSIS).read_bytes() == b"analysis" * 100
    assert (drive / previews_location.MARKER).is_file()
    assert not (drive / CUT).exists() and not (data / CUT).exists(), "cuts are made again, not moved"
    assert not (data / "ten_a").exists(), "the old account folder goes once empty"
    assert (data / "postgres/PG_VERSION").is_file() and (data / "cache/lumiverb/analysis").is_dir()
    state = move.read_state(data)
    assert (state["done_files"], state["total_files"]) == (3, 3) and state["error"] is None and state["finished_at"]
    assert json.loads((data / previews_location.FILE).read_text()) == {
        "previews": str(drive), "moving_from": None, "libraries": {}, "libraries_moving_from": {}}
    assert LocalStorage(str(data)).abs_path(PROXY) == drive / PROXY


def test_and_back_to_the_data_folder(install):
    data, drive = install
    move.start(data, drive, background=False)
    move.start(data, data, background=False)
    assert previews_location.read(data) == previews_location.Location(data)
    assert (data / PROXY).read_bytes() == b"proxy" and not (drive / "ten_a").exists()
    assert json.loads((data / previews_location.FILE).read_text())["previews"] is None


def test_a_file_written_meanwhile_is_newer_and_kept(install):
    data, drive = install
    _moving(data, drive)
    LocalStorage(str(data)).write(PROXY, b"remade")
    move.run(data)
    assert (drive / PROXY).read_bytes() == b"remade" and not (data / PROXY).exists()


def test_across_disks_it_copies_then_links_in(install, monkeypatch):
    data, drive = install
    real_link = os.link

    def no_links_across(src, dst, *a, **k):
        if Path(src).is_relative_to(data):
            raise OSError(errno.EXDEV, "Cross-device link")
        return real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", no_links_across)
    move.start(data, drive, background=False)
    assert (drive / ANALYSIS).read_bytes() == b"analysis" * 100
    assert not list(drive.rglob("*" + move.MOVING_SUFFIX))


def test_on_a_disk_without_links_it_still_never_writes_over_a_newer_file(install, monkeypatch):
    """exFAT: no hard links at all."""
    data, drive = install

    def no_links(*a, **k):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    _moving(data, drive)
    LocalStorage(str(data)).write(PROXY, b"remade")
    move.run(data)
    assert (drive / PROXY).read_bytes() == b"remade" and (drive / THUMB).read_bytes() == b"thumb"


def test_a_full_drive_stops_it_saying_so_and_it_goes_on_after(install, monkeypatch):
    data, drive = install
    real = move._move_one
    calls = []

    def full_after_one(src, dest):
        calls.append(src)
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device", str(dest))
        return real(src, dest)

    monkeypatch.setattr(move, "_move_one", full_after_one)
    with pytest.raises(move.MoveError, match="The new drive is full"):
        move.start(data, drive, background=False)
    state = move.read_state(data)
    assert state["error"].startswith("The new drive is full") and state["failed_at"]
    assert previews_location.read(data).moving_from == data, "still moving: reads find both"
    assert LocalStorage(str(data)).exists(PROXY) and LocalStorage(str(data)).exists(ANALYSIS)
    monkeypatch.setattr(move, "_move_one", real)
    move.run(data)
    assert previews_location.read(data) == previews_location.Location(drive)
    assert move.read_state(data)["error"] is None
    assert all((drive / k).is_file() for k in (PROXY, THUMB, ANALYSIS))


def test_an_unplugged_drive_stops_it_and_nothing_is_taken_for_moved(install):
    data, drive = install
    _moving(data, drive)
    (drive / previews_location.MARKER).unlink()
    with pytest.raises(move.MoveError, match="The new folder isn't there"):
        move.run(data)
    assert (data / PROXY).is_file() and previews_location.read(data).moving_from == data


def test_the_old_drive_gone_isn_t_taken_for_files_deleted(install, tmp_path):
    data, drive = install
    move.start(data, drive, background=False)  # now on the drive
    other = tmp_path / "Volumes" / "Other"
    other.mkdir()
    (other / previews_location.MARKER).write_text("")
    previews_location.write(data, other, drive)
    (drive / previews_location.MARKER).unlink()  # the old drive unplugged
    with pytest.raises(move.MoveError, match="The old folder isn't there"):
        move.run(data)
    assert previews_location.read(data).moving_from == drive


def test_upkeep_goes_on_15_minutes_after_an_error_try_again_at_once(install, monkeypatch):
    data, drive = install
    _moving(data, drive)
    started = []
    monkeypatch.setattr(move, "run", lambda d: started.append(d))
    move._update_state(data, error="The new drive is full.", failed_at=datetime.now(UTC).isoformat())
    assert move.go_on(data, waits=True) == "waiting"
    assert move.go_on(data) == "started"  # Try again
    later = (datetime.now(UTC) + timedelta(minutes=16)).timestamp()
    assert move.go_on(data, waits=True, now=later) == "started"
    with move._one_at_a_time(data):
        assert move.go_on(data) == "running"
    previews_location.write(data, drive, None)
    assert move.go_on(data) == "not moving"


def test_one_move_at_a_time(install, tmp_path):
    data, drive = install
    _moving(data, drive)
    other = tmp_path / "Volumes" / "Other"
    other.mkdir()
    with pytest.raises(move.MoveError, match="being moved already"):
        move.start(data, other, background=False)


@pytest.mark.parametrize(("folder", "said"), [
    ("", "Say which folder"),
    ("relative", "full path"),
    ("/no/such/drive", "There's no drive at /no. Is it plugged in"),
])
def test_a_folder_it_cant_use_is_refused_with_why(install, folder, said):
    data, _ = install
    with pytest.raises(move.MoveError, match=said):
        move.check_target(data, folder)


def test_a_folder_not_there_on_a_drive_that_is_says_so(install, tmp_path):
    data, _ = install
    with pytest.raises(move.MoveError, match="There's no folder at"):
        move.check_target(data, tmp_path / "nowhere")


def test_not_where_they_are_nor_inside_them(install):
    data, _ = install
    with pytest.raises(move.MoveError, match="already"):
        move.check_target(data, data)
    inside = data / "ten_a" / "elsewhere"
    inside.mkdir()
    with pytest.raises(move.MoveError, match="inside the previews"):
        move.check_target(data, inside)
    sub = data / "previews"
    sub.mkdir()
    assert move.check_target(data, sub) == sub, "a folder of its own in the data folder is fine"


def test_not_without_room_on_another_disk(install, monkeypatch):
    data, drive = install
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(move, "same_disk", lambda a, b: False)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10**9, 10**9, 10))
    with pytest.raises(move.MoveError, match="There isn't room"):
        move.check_target(data, drive, move.Size(3, 1000))


def test_what_a_move_carries_leaves_out_playback_cuts(install):
    data, _ = install
    assert move.size_of(data) == move.Size(3, len(b"proxy") + len(b"thumb") + 800)


# -- Waiting for a move -------------------------------------------------------------------


def test_cleanup_waits_for_a_move(install, monkeypatch):
    from src.server.config import get_settings
    from src.server.search import cleanup

    data, drive = install
    monkeypatch.setenv("DATA_DIR", str(data))
    get_settings.cache_clear()
    try:
        assert cleanup.previews_folder() == (data, None)
        _moving(data, drive)
        assert cleanup.previews_folder() == (None, cleanup.MOVING)
    finally:
        get_settings.cache_clear()


def test_missing_files_aren_t_repaired_during_a_move(install, monkeypatch):
    from unittest.mock import MagicMock

    from src.server import missing_artifacts

    data, drive = install
    _moving(data, drive)
    monkeypatch.setattr(missing_artifacts, "get_storage", lambda: LocalStorage(str(data)))
    session = MagicMock()
    session.execute.return_value.scalar.return_value = json.dumps(["ast_1:proxy"])
    assert missing_artifacts.repair(session) == {"checked": 0, "cleared": 0, "remade": 0}
    session.rollback.assert_called_once()


# -- Never scanned -------------------------------------------------------------------------


def test_a_library_walk_never_goes_into_the_data_or_previews_folder(tmp_path):
    """Previews moved onto a library's drive, even inside its folder."""
    from src.processing import machine
    from src.processing.ingest import _walk_library

    library = tmp_path / "Media"
    (library / "Trips").mkdir(parents=True)
    (library / "Trips" / "beach.jpg").write_bytes(b"x")
    data = library / "lumiverb-data"
    previews = library / "Lumiverb previews"
    for key in (PROXY, ANALYSIS):
        for root in (data, previews):
            (root / key).parent.mkdir(parents=True, exist_ok=True)
            (root / key).write_bytes(b"x")
    (previews / previews_location.MARKER).write_text("")
    previews_location.write(data, previews, None)
    machine.use(machine.Machine(refused_roots=(str(data),), data_dir=str(data)))
    try:
        found = [f["rel_path"] for f in _walk_library(library)]
    finally:
        machine.use(None)
    assert found == ["Trips/beach.jpg"]


# -- Said on the Admin page --------------------------------------------------------------


def test_the_disk_row_with_previews_elsewhere():
    now = datetime.now(UTC)
    gb = 1024**3
    row = h.disk_row(free=100 * gb, total=500 * gb, now=now,
                     previews={"folder": "/Volumes/Media/Lumiverb", "available": True, "same_disk": False,
                               "free": 20 * gb, "total": 4000 * gb, "moving": None})
    assert row.state == "red" and row.reason == "Data: 100.0 GB free of 500.0 GB (20%). Previews: 20.0 GB free of 3.9 TB (0%)."
    assert row.link == "/settings/storage"
    row = h.disk_row(free=100 * gb, total=500 * gb, now=now,
                     previews={"folder": "/Volumes/Media/Lumiverb", "available": False, "same_disk": False})
    assert row.state == "red" and row.reason == ("The previews folder isn't there: /Volumes/Media/Lumiverb. "
                                                 "Is its drive plugged in?")
    row = h.disk_row(free=100 * gb, total=500 * gb, now=now,
                     previews={"folder": "/x", "available": True, "same_disk": True, "moving": 0.4})
    assert row.state == "green" and row.reason.endswith("Moving the previews: 40%.")
    assert h.disk_row(free=100 * gb, total=500 * gb, now=now).reason == "100.0 GB free of 500.0 GB (20%)."


# -- A library's own folder -------------------------------------------------------------


def test_a_library_with_a_folder_of_its_own(install):
    data, drive = install
    move.start(data, drive, library_id="lib_2", background=False)
    location = previews_location.read(data)
    assert location.libraries == {"lib_2": drive} and not location.moving
    assert (drive / ANALYSIS).read_bytes() == b"analysis" * 100 and not (data / ANALYSIS).exists()
    assert (data / PROXY).is_file() and not (drive / PROXY).exists(), "another library stays"
    assert (data / "ten_a").is_dir(), "the account folder keeps the others"
    storage = LocalStorage(str(data))
    assert storage.abs_path(ANALYSIS) == drive / ANALYSIS and storage.write_path(ANALYSIS) == drive / ANALYSIS
    assert storage.write_path(PROXY) == data / PROXY and storage.write_path(CUT) == data / CUT
    state = move.read_state(data)
    assert state["library_id"] == "lib_2" and (state["done_files"], state["total_files"]) == (1, 1)


def test_the_server_s_move_leaves_a_library_s_own_folder_alone(install, tmp_path):
    data, drive = install
    move.start(data, drive, library_id="lib_2", background=False)
    elsewhere = tmp_path / "Volumes" / "Other"
    elsewhere.mkdir()
    move.start(data, elsewhere, background=False)
    assert (elsewhere / PROXY).is_file() and (drive / ANALYSIS).is_file()
    assert not (elsewhere / ANALYSIS).exists()
    assert previews_location.read(data).libraries == {"lib_2": drive}
    assert move.size_of(elsewhere) == move.Size(2, len(b"proxy") + len(b"thumb"))


def test_and_back_to_the_server_s_folder(install):
    data, drive = install
    move.start(data, drive, library_id="lib_2", background=False)
    move.start(data, None, library_id="lib_2", background=False)
    assert previews_location.read(data).libraries == {}
    assert (data / ANALYSIS).is_file() and not (drive / "ten_a").exists()


def test_during_a_library_s_move_only_its_files_are_read_from_two_places(install):
    data, drive = install
    (drive / previews_location.MARKER).write_text("")
    previews_location.write(data, data, None, libraries={"lib_2": drive}, libraries_moving_from={"lib_2": data})
    storage = LocalStorage(str(data))
    assert storage.paths(ANALYSIS) == [drive / ANALYSIS, data / ANALYSIS]
    assert storage.paths(PROXY) == [data / PROXY]
    assert storage.abs_path(ANALYSIS) == data / ANALYSIS, "not moved yet"
    move.run(data)
    assert storage.abs_path(ANALYSIS) == drive / ANALYSIS and not previews_location.read(data).moving


def test_a_library_s_folder_unplugged_stops_only_its_writes(install):
    data, drive = install
    move.start(data, drive, library_id="lib_2", background=False)
    (drive / previews_location.MARKER).unlink()
    storage = LocalStorage(str(data))
    with pytest.raises(FileNotFoundError, match="plugged in"):
        storage.write(ANALYSIS, b"x")
    storage.write(PROXY, b"fine")
    assert (data / PROXY).read_bytes() == b"fine"


def test_missing_files_aren_t_repaired_when_a_library_s_drive_is_unplugged(install):
    from src.server import missing_artifacts

    data, drive = install
    move.start(data, drive, library_id="lib_2", background=False)
    storage = LocalStorage(str(data))
    assert not missing_artifacts._storage_gone(storage, ANALYSIS)
    shutil.rmtree(drive / "ten_a")
    (drive / previews_location.MARKER).unlink()  # unplugged: an empty mount point
    assert missing_artifacts._storage_gone(storage, ANALYSIS)
    assert not missing_artifacts._storage_gone(storage, PROXY), "the other library's are judged as before"


def test_a_library_can_t_move_where_it_is(install):
    data, drive = install
    with pytest.raises(move.MoveError, match="already"):
        move.plan(data, None, library_id="lib_2")  # the server's folder: where it is


def test_cleanup_visits_a_library_s_own_folder(install, monkeypatch):
    from src.server.config import get_settings
    from src.server.search import cleanup

    data, drive = install
    move.start(data, drive, library_id="lib_2", background=False)
    monkeypatch.setenv("DATA_DIR", str(data))
    get_settings.cache_clear()
    try:
        assert cleanup.library_folders("ten_a") == [("lib_2", drive)]
        assert cleanup.library_folders("ten_b") == []
        (drive / previews_location.MARKER).unlink()
        assert cleanup.library_folders("ten_a") == [], "unplugged: nothing to clean there"
    finally:
        get_settings.cache_clear()


def test_a_library_walk_never_goes_into_a_library_s_previews_folder(tmp_path):
    from src.processing import machine
    from src.processing.ingest import _walk_library

    library = tmp_path / "Media"
    (library / "a.jpg").parent.mkdir(parents=True)
    (library / "a.jpg").write_bytes(b"x")
    own = library / "Trips previews"
    (own / ANALYSIS).parent.mkdir(parents=True)
    (own / ANALYSIS).write_bytes(b"x")
    data = tmp_path / "data"
    previews_location.write(data, data, None, libraries={"lib_2": own})
    machine.use(machine.Machine(data_dir=str(data)))
    try:
        assert [f["rel_path"] for f in _walk_library(library)] == ["a.jpg"]
    finally:
        machine.use(None)


def test_the_disk_row_says_a_library_s_drive_is_unplugged():
    now = datetime.now(UTC)
    gb = 1024**3
    row = h.disk_row(free=100 * gb, total=500 * gb, now=now,
                     previews={"folder": "/d", "available": True, "same_disk": True, "moving": None,
                               "missing": ["/Volumes/Video"]})
    assert row.state == "red" and row.reason == ("A library's previews folder isn't there: /Volumes/Video. "
                                                 "Is its drive plugged in?")
    assert row.link == "/settings/storage"


def test_a_look_at_whether_a_move_runs_never_stops_one_starting(install, monkeypatch):
    """running() takes the lock for an instant to see whether it's free. A
    mover starting in that instant used to give up ("being moved already")
    and move nothing, silently: Settings › Storage polls running() every 2 s,
    and Try again's answer polls it as the mover starts (a full run under
    load caught it: the move never finished)."""
    import threading

    data, drive = install
    _moving(data, drive)
    monkeypatch.setattr(move, "LOOK_WAIT_SEC", 60)  # however slow the machine, the look is let go first
    with move._one_at_a_time(data):  # a look, held a moment as the mover starts
        mover = threading.Thread(target=move.run, args=(data,))
        mover.start()
        time.sleep(0.2)
    mover.join(60)
    assert not mover.is_alive()
    assert previews_location.read(data) == previews_location.Location(drive), "it waited its turn and moved"
    assert (drive / PROXY).is_file()


def test_a_second_mover_still_finds_one_running(install, monkeypatch):
    """Waiting out a look isn't waiting out a move: one that holds the lock
    longer than a look is said to be running."""
    data, drive = install
    _moving(data, drive)
    monkeypatch.setattr(move, "LOOK_WAIT_SEC", 0.2)
    with move._one_at_a_time(data), pytest.raises(move.MoveError, match="being moved already"):
        move.run(data)
