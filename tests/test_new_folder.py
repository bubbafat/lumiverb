"""A folder an admin names that isn't there yet (src/server/api/new_folder.py):
asked about first, made when the request says so, never where a drive that
isn't plugged in would be, and taken away again when the request doesn't go
through. The routes that use it: tests/test_storage_api.py,
tests/test_library_previews_api.py, tests/test_backups_api.py."""

from __future__ import annotations

import os

import pytest

from src.server.api.errors import ApiError, DecisionRequiredError
from src.server.api.new_folder import made_if_asked

pytestmark = pytest.mark.fast


def test_one_not_there_is_asked_about_and_nothing_is_made(tmp_path):
    wanted = tmp_path / "Lumiverb" / "previews"
    with pytest.raises(DecisionRequiredError) as asked, made_if_asked(str(wanted), False, tmp_path):
        pass
    assert asked.value.code == "make_folder"
    assert asked.value.message == f"There's no folder at {wanted}. Make it? It'd be on the same drive as Lumiverb's data."
    assert asked.value.details["folder"] == str(wanted) and asked.value.details["same_drive_as_data"] is True
    assert asked.value.details["free"] > 0
    assert not (tmp_path / "Lumiverb").exists()


def test_said_yes_it_is_made_with_the_folders_above_it(tmp_path):
    wanted = tmp_path / "Lumiverb" / "previews"
    with made_if_asked(f"  {wanted}  ", True, tmp_path):
        assert wanted.is_dir()
    assert wanted.is_dir(), "the request went through: it stays"


def test_a_request_that_doesnt_go_through_takes_away_what_it_made(tmp_path):
    (tmp_path / "Lumiverb").mkdir()
    (tmp_path / "Lumiverb" / "kept.txt").write_text("someone's")
    wanted = tmp_path / "Lumiverb" / "a" / "b"
    with pytest.raises(RuntimeError), made_if_asked(str(wanted), True, tmp_path):
        assert wanted.is_dir()
        raise RuntimeError("no room")
    assert not (tmp_path / "Lumiverb" / "a").exists()
    assert (tmp_path / "Lumiverb" / "kept.txt").read_text() == "someone's", "only what it made goes"


def test_one_something_was_written_into_meanwhile_stays(tmp_path):
    wanted = tmp_path / "previews"
    with pytest.raises(RuntimeError), made_if_asked(str(wanted), True, tmp_path):
        (wanted / "a file").write_text("")
        raise RuntimeError("refused")
    assert (wanted / "a file").exists()


def test_never_where_a_drive_that_isnt_plugged_in_would_be(tmp_path):
    for raw, drive in (("/Volumes/LumiverbNoSuchDrive/previews", "/Volumes/LumiverbNoSuchDrive"),
                       ("/lumiverb-no-such-drive/previews", "/lumiverb-no-such-drive")):
        if not os.path.isdir("/Volumes") and raw.startswith("/Volumes"):
            drive = "/Volumes"  # not a Mac: nothing at /Volumes either
        with pytest.raises(ApiError) as refused, made_if_asked(raw, True, tmp_path):
            pass
        assert refused.value.status_code == 400 and refused.value.code == "folder_not_usable"
        assert refused.value.message == f"There's no drive at {drive}. Is it plugged in?"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_one_it_cant_make_says_where(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(ApiError) as refused, made_if_asked(str(locked / "previews"), True, tmp_path):
            pass
    finally:
        locked.chmod(0o700)
    assert refused.value.status_code == 400 and refused.value.message == f"Lumiverb can't make a folder in {locked}."


def test_one_thats_there_or_isnt_a_full_path_is_left_to_the_caller(tmp_path):
    for raw in (str(tmp_path), "relative/previews", "", "   ", None):
        with made_if_asked(raw, False, tmp_path):
            pass
    assert sorted(p.name for p in tmp_path.iterdir()) == []
