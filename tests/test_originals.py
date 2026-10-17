"""A library's originals through their interface (ADR-020 decision 5,
src/processing/originals.py): reached when they can be read now, then
walked, stat'ed and held as files for a block, never written, and never a
path outside the library. The walk's own rules (filters, links, refused
folders, names) are tested where the scan relies on them
(test_scan_safety.py, test_ingest_dimensions.py, test_previews_location.py)."""

from __future__ import annotations

import os
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.processing import machine
from src.processing.machine import Machine
from src.processing.originals import Folder, Originals, Stat, reach, where
from src.shared.io_utils import UnsafeRelPathError

pytestmark = pytest.mark.fast


@pytest.fixture
def library(tmp_path: Path) -> dict:
    root = tmp_path / "Footage"
    (root / "Day 1").mkdir(parents=True)
    (root / "Day 1" / "a.jpg").write_bytes(b"jpeg")
    (root / "b.mov").write_bytes(b"mov!!")
    machine.use(Machine(root_map={"/": "/"}))
    return {"library_id": "lib_1", "name": "Footage", "root_path": str(root)}


def test_a_library_that_can_be_read_is_reached_and_the_same_look_twice_is_equal(library):
    originals = reach(library, require_entries=True)
    assert isinstance(originals, Originals)
    assert originals == reach(library), "a later look at the same folder is the same originals"
    assert originals.library_id == "lib_1"
    assert originals.where == str(Path(library["root_path"]).resolve())


def test_one_that_cant_isnt(library, tmp_path):
    assert reach({**library, "root_path": str(tmp_path / "not-mounted")}) is None
    (tmp_path / "empty").mkdir()
    empty = {**library, "root_path": str(tmp_path / "empty")}
    assert reach(empty) is not None
    assert reach(empty, require_entries=True) is None, "an unmounted mount point is an empty folder"


def test_the_scheduler_never_reaches_a_library_a_device_reads(library):
    machine.use(Machine(root_map={"/": "/"}, mapped_roots_only=True))
    assert reach(library) is not None
    assert reach({**library, "reader": "server"}) is not None
    assert reach({**library, "reader": "device_1"}) is None


def test_where_says_where_they_would_be_and_reads_nothing(tmp_path):
    lib = {"library_id": "lib_1", "root_path": "/Volumes/media-01/Footage"}
    assert where(lib, {"/Volumes/media-01": str(tmp_path / "nowhere")}) == tmp_path / "nowhere" / "Footage"


def test_stat_says_what_is_there(library):
    originals = reach(library)
    a = originals.stat("Day 1/a.jpg")
    assert isinstance(a, Stat) and (a.size, a.file, a.folder) == (4, True, False)
    assert a.mtime == datetime.fromtimestamp(os.stat(Path(library["root_path"], "Day 1", "a.jpg")).st_mtime, UTC)
    day = originals.stat("Day 1")
    assert day.folder and not day.file
    assert originals.stat("").folder, '"" is the library'
    assert originals.stat("Day 1/missing.jpg") is None
    with pytest.raises(UnsafeRelPathError):
        originals.stat("../outside.jpg")


def test_a_file_is_held_for_the_block_as_the_original_itself(library):
    originals = reach(library)
    with originals.local_file("Day 1/a.jpg") as path:
        assert path.read_bytes() == b"jpeg"
        assert path.samefile(Path(library["root_path"], "Day 1", "a.jpg")), "never a copy"


@pytest.mark.parametrize("rel_path", ["Day 1/missing.jpg", "Day 1", "nope/x.jpg"])
def test_what_isnt_a_file_there_is_not_found(library, rel_path):
    with pytest.raises(FileNotFoundError), reach(library).local_file(rel_path):
        pass


@pytest.mark.parametrize("rel_path", ["../outside.jpg", "/etc/hosts", "Day 1/../../outside.jpg", "a\nb.jpg"])
def test_a_path_that_would_leave_the_library_is_refused(library, tmp_path, rel_path):
    (tmp_path / "outside.jpg").write_bytes(b"not the library's")
    with pytest.raises(UnsafeRelPathError), reach(library).local_file(rel_path):
        pass


def test_a_link_out_of_the_library_is_refused(library, tmp_path):
    (tmp_path / "outside.jpg").write_bytes(b"not the library's")
    Path(library["root_path"], "link.jpg").symlink_to(tmp_path / "outside.jpg")
    with pytest.raises(UnsafeRelPathError), reach(library).local_file("link.jpg"):
        pass


def test_a_name_kept_in_the_other_unicode_form_is_found(library):
    nfc = unicodedata.normalize("NFC", "Zürich.jpg")
    Path(library["root_path"], unicodedata.normalize("NFD", nfc)).write_bytes(b"z")
    originals = reach(library)
    with originals.local_file(nfc) as path:
        assert path.read_bytes() == b"z"
    assert originals.stat(nfc).file


def test_the_walk_lists_media_files_with_their_facts(library):
    walked = reach(library).walk()
    assert [(f["rel_path"], f["file_size"], f["media_type"], f["ext"]) for f in walked] == [
        ("Day 1/a.jpg", 4, "image", ".jpg"), ("b.mov", 5, "video", ".mov")]
    assert all(f["file_mtime"].tzinfo is UTC for f in walked)
    assert [f["rel_path"] for f in reach(library).walk("Day 1")] == ["Day 1/a.jpg"]


def test_a_scope_that_would_leave_the_library_walks_nothing_and_holds_back_deletions(library):
    unlisted: list[str] = []
    assert reach(library).walk("../elsewhere", unlisted=unlisted) == []
    assert unlisted == ["../elsewhere"]


def test_the_drive_they_are_on(library):
    assert reach(library).device(timeout=5) == os.stat(library["root_path"]).st_dev
    assert Folder(Path(library["root_path"]) / "gone", "lib_1").device(timeout=5) is None
