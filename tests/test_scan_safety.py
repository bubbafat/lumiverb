"""Scanning is safe from a network mount (ADR-016 phase 0).

- rel_path is Unicode NFC, as the macOS scanner stores it, so the same file
  has one identity whichever machine scans it.
- Source files are found on disk even when their names are stored in
  another normalization form (macOS-created NFD names on a Linux mount).
- Files that look missing make the scan look at the storage again first:
  gone, nothing is archived or moved.
- What the walk saw, and couldn't, is what the server is told: a folder it
  couldn't list, a file still being written, an empty file.

The server decides what's gone (tests/test_scans_api.py); these tests say
what the scan tells it and when it stops, over a scripted server
(tests/scan_fakes.py).
"""

from __future__ import annotations

import errno
import logging
import os
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.processing.originals import Folder
from src.processing.scan import Previews, ScanStats, run_scan
from src.shared.io_utils import resolve_source_path
from src.shared.path_filter import PathFilter
from tests.scan_fakes import SCAN_ID, FakeScans

NFC = unicodedata.normalize("NFC", "Café/résumé.jpg")
NFD = unicodedata.normalize("NFD", NFC)


@pytest.fixture(autouse=True)
def _cache_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan's proxy cache in the test's own folder, never ~/.cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _write(root: Path, rel: str, data: bytes = b"jpeg") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.fast
def test_walk_library_stores_nfc_rel_path(tmp_path: Path) -> None:
    _write(tmp_path, NFD)

    [entry] = Folder(tmp_path).walk()

    assert entry["rel_path"] == NFC
    assert entry["rel_path"] != NFD


@pytest.mark.fast
@pytest.mark.parametrize(
    "on_disk",
    [
        NFD,
        unicodedata.normalize("NFC", "Café") + "/" + unicodedata.normalize("NFD", "résumé.jpg"),
        unicodedata.normalize("NFD", "Café") + "/" + unicodedata.normalize("NFC", "résumé.jpg"),
    ],
)
def test_resolve_source_path_finds_other_normalization(tmp_path: Path, on_disk: str) -> None:
    _write(tmp_path, on_disk, b"original bytes")

    path = resolve_source_path(tmp_path, NFC)

    assert path.read_bytes() == b"original bytes"


@pytest.mark.fast
def test_resolve_source_path_plain_hit_and_miss(tmp_path: Path) -> None:
    _write(tmp_path, "a/b.jpg")

    assert resolve_source_path(tmp_path, "a/b.jpg") == tmp_path / "a/b.jpg"
    assert resolve_source_path(tmp_path, "a/missing.jpg") == tmp_path / "a/missing.jpg"
    assert resolve_source_path(tmp_path, "nope/x.jpg") == tmp_path / "nope/x.jpg"


def _local(rel: str, age_sec: float, size: int = 4) -> dict:
    return {"rel_path": rel, "file_size": size, "media_type": "image", "ext": ".jpg",
            "file_mtime": datetime.now(UTC) - timedelta(seconds=age_sec)}


def _scan(server: FakeScans, root: Path, *, local: list[dict] | None = None, walk=None,
          library_filters: list | None = None, **kwargs) -> ScanStats:
    """run_scan as the scheduler runs it (moves applied), over real files
    under root unless the walk's answer is given."""
    patches = [
        patch("src.processing.scan._told", return_value=([], library_filters or [], Previews())),
    ]
    if local is not None or walk is not None:
        patches.append(patch.object(Folder, "walk", side_effect=walk or (lambda *a, **k: local)))
    for p in patches:
        p.start()
    try:
        return run_scan(server.client(), {"library_id": "lib_1", "root_path": str(root)},
                        console=Console(quiet=True), allow_moves=True, **kwargs)
    finally:
        for p in patches:
            p.stop()


def _library(tmp_path: Path, on_disk: int = 10) -> tuple[Path, list[dict]]:
    """A library on a mounted share (something in it) and its walk: on_disk files, unchanged."""
    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    (root / "f0.jpg").touch()
    return root, [_local(f"f{i}.jpg", 600) for i in range(on_disk)]


# ---------------------------------------------------------------------------
# The storage is looked at again before anything is archived or moved
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_a_mount_gone_when_files_look_missing_stops_the_scan(tmp_path: Path) -> None:
    """Files that look missing make the scan check the mount again; if it has
    gone (a network drop, a clean unmount mid-walk), the scan stops and
    archives nothing (Robert's rule, Oct 8). The worker keeps the changes."""
    root, local = _library(tmp_path)
    server = FakeScans(default=("unchanged", None, "ast_1"), gone=90)
    with patch("src.processing.roots.reachable_root", side_effect=[root, None]) as probe:
        stats = _scan(server, root, local=local, allow_mass_delete=True)

    assert server.finishes() == [] and server.abandoned()
    assert stats.root_unreachable
    assert probe.call_args_list[1].kwargs["require_entries"] is True


@pytest.mark.fast
def test_files_missing_from_a_mount_still_there_are_archived(tmp_path: Path) -> None:
    root, local = _library(tmp_path)
    server = FakeScans(default=("unchanged", None, "ast_1"), gone=90)
    with patch("src.processing.roots.reachable_root", side_effect=[root, root]):
        stats = _scan(server, root, local=local, allow_mass_delete=True)

    assert server.finishes() == [{"allow_mass_missing": True}]
    assert stats.deleted == 90 and not stats.root_unreachable


@pytest.mark.fast
def test_a_scan_with_nothing_missing_doesnt_check_the_mount_again(tmp_path: Path) -> None:
    root, local = _library(tmp_path, on_disk=100)
    server = FakeScans(default=("unchanged", None, "ast_1"), gone=0)
    with patch("src.processing.roots.reachable_root", return_value=root) as probe:
        _scan(server, root, local=local)

    assert probe.call_count == 1
    assert len(server.finishes()) == 1


@pytest.mark.fast
def test_a_mount_gone_before_a_move_is_made_moves_nothing(tmp_path: Path) -> None:
    """A move is made when its hash is sent (the scheduler applies moves):
    the storage is looked at first."""
    root, local = _library(tmp_path, on_disk=1)
    _write(root, "moved/f1.jpg")
    local.append(_local("moved/f1.jpg", 600))
    server = FakeScans(plan={"moved/f1.jpg": ("hash", "move", None)}, default=("unchanged", None, "ast_0"), gone=1)
    with patch("src.processing.roots.reachable_root", side_effect=[root, None]):
        stats = _scan(server, root, local=local)

    assert server.hashed() == [] and server.finishes() == [] and server.abandoned()
    assert stats.root_unreachable


@pytest.mark.fast
def test_an_unmounted_share_mid_scan_archives_nothing(tmp_path: Path, monkeypatch) -> None:
    """End to end through the real mount check: fstab lists the share, it's
    mounted when the scan starts, and it goes away during the walk."""
    from src.processing import roots

    root = tmp_path / "lib"
    root.mkdir()
    (root / "f0.jpg").touch()
    fstab = tmp_path / "fstab"
    fstab.write_text(f"//nas/share {root} cifs soft 0 0\n")
    monkeypatch.setattr(roots, "FSTAB", fstab)
    mounted = {str(root)}
    monkeypatch.setattr(roots.os.path, "ismount", lambda p: str(p) in mounted)
    local = [_local(f"f{i}.jpg", 600) for i in range(10)]

    def walk(*args, **kwargs):
        mounted.clear()  # unmounted mid-walk: the files not yet seen look missing
        return local

    server = FakeScans(default=("unchanged", None, "ast_1"), gone=90)
    stats = _scan(server, root, walk=walk, allow_mass_delete=True)

    assert server.finishes() == []
    assert stats.root_unreachable


@pytest.mark.fast
def test_a_share_gone_before_the_walk_found_anything_keeps_the_changes(tmp_path: Path) -> None:
    """An empty walk on a library that had files: the storage is checked
    again, and if it's gone the scan says so (the worker then keeps the
    change reports for when it's back)."""
    root, _ = _library(tmp_path)
    server = FakeScans(gone=100)
    with patch("src.processing.roots.reachable_root", side_effect=[root, None]):
        stats = _scan(server, root, local=[], allow_mass_delete=True)
    assert server.finishes() == []
    assert stats.root_unreachable


@pytest.mark.fast
def test_a_library_with_no_media_on_a_present_share_is_just_empty(tmp_path: Path) -> None:
    # The server holds back archiving when a whole library's scan finds
    # nothing (tests/test_scans_api.py); the scan just says what it found.
    root, _ = _library(tmp_path)
    server = FakeScans(gone=0)
    with patch("src.processing.roots.reachable_root", side_effect=[root, root]):
        stats = _scan(server, root, local=[])
    assert not stats.root_unreachable
    assert server.found() == [] and len(server.finishes()) == 1


@pytest.mark.fast
def test_path_prefix_is_nfc() -> None:
    from src.shared.io_utils import normalize_path_prefix

    assert normalize_path_prefix("/" + unicodedata.normalize("NFD", "Café/") ) == unicodedata.normalize("NFC", "Café")


# ---------------------------------------------------------------------------
# Files still being written (ADR-016 phase 2): the server scans as soon as the
# Mac reports a copy, so a file still growing waits for a later scan, as the
# macOS scanner's 30-second quarantine does. It's on disk all the same: never
# taken for missing.
# ---------------------------------------------------------------------------


def _settling(server: FakeScans) -> dict[str, bool]:
    return {f["rel_path"]: f["settling"] for f in server.found()}


@pytest.mark.fast
def test_files_still_being_written_are_listed_as_settling(tmp_path: Path) -> None:
    root, _ = _library(tmp_path)
    server = FakeScans(default=("unchanged", None, "ast_1"))
    stats = _scan(server, root, local=[_local("done.jpg", 600), _local("copying.jpg", 2)])
    # Listed, so never gone; settling, so the server plans nothing for it.
    assert _settling(server) == {"done.jpg": False, "copying.jpg": True}
    assert stats.settling == 1
    assert stats.settling_paths == ["copying.jpg"]


@pytest.mark.fast
def test_a_camera_clock_far_ahead_is_not_taken_for_a_copy(tmp_path: Path) -> None:
    # Stamped an hour in the future, it would otherwise wait an hour.
    root, _ = _library(tmp_path)
    server = FakeScans(default=("unchanged", None, "ast_1"))
    _scan(server, root, local=[_local("ahead.jpg", -3600), _local("copying.jpg", -60)])
    assert _settling(server) == {"ahead.jpg": False, "copying.jpg": True}


@pytest.mark.fast
def test_an_empty_file_is_on_disk_but_not_ingested(tmp_path: Path) -> None:
    """A file truncated mid-rewrite is 0 bytes: it's listed (so its clip
    isn't archived), never settling, and the server plans nothing for it."""
    root, _ = _library(tmp_path)
    server = FakeScans(default=("unchanged", None, "ast_1"))
    local = [_local("f0.jpg", 2, size=0), _local("f1.jpg", 600)]
    stats = _scan(server, root, local=local)
    assert [(f["rel_path"], f["file_size"], f["settling"]) for f in server.found()] == [
        ("f0.jpg", 0, False), ("f1.jpg", 4, False)]
    assert stats.settling == 0 and server.ingested() == []


@pytest.mark.fast
def test_scan_names_the_files_that_failed(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    (root / "bad.jpg").write_bytes(b"jpeg")
    os.utime(root / "bad.jpg", (1e9, 1e9))
    server = FakeScans()
    with patch("src.processing.scan._generate_proxy_bytes", side_effect=RuntimeError("not a JPEG")):
        stats = _scan(server, root)
    assert stats.failed == 1
    assert stats.failed_paths == ["bad.jpg"]


# ---------------------------------------------------------------------------
# A scan sees the folder it was given (ADR-016 phase 2): the worker
# acknowledges reports after the scan, so a walk that quietly finds nothing
# would drop them.
# ---------------------------------------------------------------------------

ZURICH = unicodedata.normalize("NFC", "Zürich")


@pytest.mark.fast
def test_walk_finds_a_folder_named_in_nfd(tmp_path: Path) -> None:
    _write(tmp_path, unicodedata.normalize("NFD", ZURICH) + "/A001.jpg")
    [entry] = Folder(tmp_path).walk(ZURICH)
    assert entry["rel_path"] == f"{ZURICH}/A001.jpg"


@pytest.mark.fast
def test_scanning_a_folder_named_in_nfd_lists_its_files(tmp_path: Path) -> None:
    # Its files' clips are compared in one Unicode form on the server
    # (tests/test_scans_api.py test_names_are_compared_in_one_unicode_form).
    root = tmp_path / "lib"
    _write(root, unicodedata.normalize("NFD", ZURICH) + "/A001.jpg")
    server = FakeScans(default=("unchanged", None, "ast_1"))
    _scan(server, root, path_prefix=ZURICH)
    assert unicodedata.normalize("NFC", server.started()["scope"]) == ZURICH
    assert [f["rel_path"] for f in server.found()] == [f"{ZURICH}/A001.jpg"]


@pytest.mark.fast
def test_a_deleted_folder_is_seen_from_its_parent(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    _write(root, "Shoots/keep.jpg")
    server = FakeScans(default=("unchanged", None, "ast_1"), gone=1)
    _scan(server, root, path_prefix="Shoots/Gone")
    assert server.started()["scope"] == "Shoots"  # its clips are the scope's, not found: gone
    assert [f["rel_path"] for f in server.found()] == ["Shoots/keep.jpg"]
    assert len(server.finishes()) == 1


@pytest.mark.fast
def test_an_emptied_folder_sees_its_deletions(tmp_path: Path) -> None:
    # An empty folder within a library is just empty, unlike the library's own.
    root = tmp_path / "lib"
    (root / "Shoots" / "Day 1").mkdir(parents=True)
    _write(root, "Other/b.jpg")
    server = FakeScans(gone=1)
    stats = _scan(server, root, path_prefix="Shoots/Day 1")
    assert server.started()["scope"] == "Shoots/Day 1"
    assert server.found() == [] and len(server.finishes()) == 1
    assert stats.deleted == 1 and not stats.root_unreachable


@pytest.mark.fast
def test_walk_lists_hidden_files_but_no_symlinks(tmp_path: Path) -> None:
    # A symlink could reach any file on the machine: it isn't the library's.
    root = tmp_path / "lib"
    for rel in ("a/f.jpg", ".h.jpg", ".hid/g.jpg", "notes.txt"):
        _write(root, rel)
    _write(tmp_path, "other/o.jpg")
    (root / "linked").symlink_to(tmp_path / "other")
    (root / "link.jpg").symlink_to(tmp_path / "other" / "o.jpg")
    (root / "broken.jpg").symlink_to(tmp_path / "nowhere.jpg")
    assert [f["rel_path"] for f in Folder(root).walk()] == [".h.jpg", ".hid/g.jpg", "a/f.jpg"]


@pytest.mark.fast
def test_walk_of_a_symlinked_prefix_lists_nothing_and_deletes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    _write(root, "a/f.jpg")
    _write(tmp_path, "other/o.jpg")
    (root / "linked").symlink_to(tmp_path / "other")
    unlisted: list[str] = []
    assert Folder(root).walk("linked", unlisted=unlisted) == []
    assert unlisted == ["linked"]


# ---------------------------------------------------------------------------
# A file or folder whose name has a control character (a newline, a tab) is
# never scanned: the server refuses the name. Said once, never a failure.
# ---------------------------------------------------------------------------

CONTROL_NAMED = ["new\nline.jpg", "tab\there.jpg", "Shoots\x1b"]


def _photo(root: Path, rel: str) -> None:
    from PIL import Image

    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), (200, 80, 40)).save(path, format="JPEG")
    os.utime(path, (1_700_000_000, 1_700_000_000))  # settled long ago, not still copying


@pytest.mark.fast
def test_a_name_with_a_newline_is_never_scanned(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    root = tmp_path / "lib"
    for rel in ("keep.jpg", "new\nline.jpg", "tab\there.jpg", "Shoots\x1b/inside.jpg", "Shoots/ok.jpg"):
        _photo(root, rel)
    server = FakeScans()
    with caplog.at_level(logging.WARNING, logger="src.processing.ingest"):
        stats = _scan(server, root)

    assert [f["rel_path"] for f in server.found()] == ["Shoots/ok.jpg", "keep.jpg"]
    assert sorted(server.ingested()) == ["Shoots/ok.jpg", "keep.jpg"]
    assert stats.new == 2
    # Skipped, not failed: nothing for the scheduler to try again, nothing held back.
    assert (stats.failed, stats.failed_paths, stats.unlisted) == (0, [], [])
    said = sorted(r.getMessage() for r in caplog.records if "control character" in r.getMessage())
    assert said == sorted(f"Not scanning {name!r}: its name has a control character" for name in CONTROL_NAMED)


@pytest.mark.fast
def test_a_clip_already_at_a_name_with_a_control_character_is_archived_as_missing(tmp_path: Path) -> None:
    """Ingested before names were checked: the scan no longer lists it, so
    the server takes its clip for gone and archives it (kept, with what
    people wrote), never deletes it."""
    root = tmp_path / "lib"
    for rel in ("keep.jpg", "new\nline.jpg", "Shoots\x1b/inside.jpg"):
        _write(root, rel)
    server = FakeScans(default=("unchanged", None, "ast_keep"), gone=2)
    stats = _scan(server, root)
    assert [f["rel_path"] for f in server.found()] == ["keep.jpg"]
    assert server.finishes() == [{"allow_mass_missing": False}] and stats.deleted == 2


@pytest.mark.fast
def test_the_walk_never_lists_a_name_with_a_control_character(tmp_path: Path) -> None:
    for rel in ("a.jpg", "a\rb.jpg", "Day\x7f/c.jpg", "Day/d.jpg", "Deep/e\x01/f.jpg"):
        _write(tmp_path, rel)
    assert [f["rel_path"] for f in Folder(tmp_path).walk()] == ["Day/d.jpg", "a.jpg"]


# ---------------------------------------------------------------------------
# A folder that can't be listed (no permission, or a CIFS soft mount timing
# out mid-walk) must not look deleted: the server is told, and takes nothing
# under it for gone, nor archives anything while there's one.
# ---------------------------------------------------------------------------

needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root reads any folder")


@pytest.fixture
def locked(tmp_path: Path):
    """A library with a folder nobody can list."""
    root = tmp_path / "lib"
    _write(root, "open/a.jpg")
    _write(root, "Locked/b.jpg")
    (root / "Locked").chmod(0)
    yield root
    (root / "Locked").chmod(0o755)


@pytest.mark.fast
@needs_permissions
def test_walk_reports_folders_it_could_not_list(locked: Path) -> None:
    unlisted: list[str] = []
    files = Folder(locked).walk(unlisted=unlisted)
    assert [f["rel_path"] for f in files] == ["open/a.jpg"]
    assert unlisted == ["Locked"]


@pytest.mark.fast
@needs_permissions
def test_a_folder_that_cannot_be_listed_is_named_to_the_server(locked: Path) -> None:
    server = FakeScans(default=("unchanged", None, "ast_a"))
    stats = _scan(server, locked)
    assert server.unlisted() == ["Locked"]
    assert stats.unlisted == ["Locked"]
    assert [f["rel_path"] for f in server.found()] == ["open/a.jpg"]


@pytest.mark.fast
@needs_permissions
def test_moves_outside_a_folder_that_cannot_be_listed_still_apply(locked: Path) -> None:
    moved = _write(locked, "open/moved.jpg", b"moved bytes")
    for f in (moved, locked / "open" / "a.jpg"):
        os.utime(f, (1e9, 1e9))  # long settled
    server = FakeScans(plan={"open/moved.jpg": ("hash", "move", None)}, default=("unchanged", None, "ast_a"),
                       gone=1, decide=lambda rel, sha: {"decision": "moved", "asset_id": "ast_moved",
                                                        "moved_from": "was/moved.jpg"})
    stats = _scan(server, locked)
    assert server.unlisted() == ["Locked"]
    assert [[f["rel_path"] for f in b] for b in server.hashed()] == [["open/moved.jpg"]]
    assert stats.moved == 1


@pytest.mark.fast
def test_a_root_gone_mid_scan_is_reported_unreachable(tmp_path: Path) -> None:
    # Checked reachable, then unmounted: nothing was scanned, so the worker
    # must keep the reports.
    client = MagicMock()
    with (
        patch("src.processing.roots.reachable_root", return_value=tmp_path / "gone"),
        patch("src.processing.scan._told", return_value=([], [], Previews())),
    ):
        stats = run_scan(client, {"library_id": "lib_1", "root_path": "/x"}, path_prefix="Day 1",
                         console=Console(quiet=True))
    assert stats.root_unreachable
    client.post.assert_not_called()  # no scan started


# ---------------------------------------------------------------------------
# A file that lists but can't be checked (no permission, or the share going
# away between listing and stat) isn't gone. Python 3.14's Path.is_file()
# says False for any error, which made a whole folder look deleted.
# ---------------------------------------------------------------------------

UNCHECKABLE = [errno.EACCES, errno.EIO]


@pytest.fixture
def flaky(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    """A library whose Flaky/ folder lists, but whose files can't be stat'ed."""
    root = tmp_path / "lib"
    _write(root, "open/a.jpg")
    _write(root, "Flaky/b.jpg")
    _write(root, "Flaky/c.jpg")
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if Path(os.fspath(path)).parent.name == "Flaky":
            raise OSError(request.param, os.strerror(request.param), os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    return root


@pytest.mark.fast
@pytest.mark.parametrize("flaky", UNCHECKABLE, indirect=True)
def test_walk_reports_a_folder_whose_files_cannot_be_checked(flaky: Path) -> None:
    unlisted: list[str] = []
    files = Folder(flaky).walk(unlisted=unlisted)
    assert [f["rel_path"] for f in files] == ["open/a.jpg"]
    assert unlisted == ["Flaky"]


@pytest.mark.fast
@pytest.mark.parametrize("flaky", UNCHECKABLE, indirect=True)
def test_files_that_cannot_be_checked_are_named_to_the_server(flaky: Path) -> None:
    server = FakeScans(gone=2)
    stats = _scan(server, flaky, path_prefix="Flaky")
    assert server.unlisted() == ["Flaky"]
    assert stats.unlisted == ["Flaky"]


@pytest.mark.fast
def test_a_file_gone_between_listing_and_stat_is_just_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "lib"
    _write(root, "Day/a.jpg")
    _write(root, "Day/b.jpg")
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if os.fspath(path).endswith("b.jpg"):
            raise FileNotFoundError(errno.ENOENT, "gone", os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    unlisted: list[str] = []
    assert [f["rel_path"] for f in Folder(root).walk(unlisted=unlisted)] == ["Day/a.jpg"]
    assert unlisted == []


@pytest.mark.fast
@pytest.mark.parametrize("err", UNCHECKABLE)
def test_a_folder_that_cannot_be_checked_is_not_taken_for_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               err: int) -> None:
    # Taken for gone, its parent would be scanned instead, and what's in it
    # deleted if the parent's walk then missed it.
    root = tmp_path / "lib"
    _write(root, "Shoots/Day 1/a.jpg")
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if Path(os.fspath(path)).name == "Day 1":
            raise OSError(err, os.strerror(err), os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    server = FakeScans()
    stats = _scan(server, root, path_prefix="Shoots/Day 1")
    assert server.requests == []  # no scan started: nothing can be taken for gone
    assert stats.unlisted == ["Shoots/Day 1"]


@pytest.fixture
def junk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    """A library with media beside files that can't be stat'ed: a .DS_Store and an excluded .jpg."""
    root = tmp_path / "lib"
    _write(root, "Day/a.jpg")
    _write(root, "Day/.DS_Store")
    _write(root, "Day/Exports/x.jpg")
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if Path(os.fspath(path)).name in (".DS_Store", "x.jpg"):
            raise OSError(request.param, os.strerror(request.param), os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    return root


EXPORTS = [PathFilter(type="exclude", pattern="**/Exports/**")]


@pytest.mark.fast
@pytest.mark.parametrize("junk", UNCHECKABLE, indirect=True)
def test_files_the_scan_would_skip_are_never_checked(junk: Path) -> None:
    # A .DS_Store that can't be stat'ed made its folder "unlisted", which
    # blocked every deletion in the library on every scan.
    unlisted: list[str] = []
    files = Folder(junk).walk(library_filters=EXPORTS, unlisted=unlisted)
    assert [f["rel_path"] for f in files] == ["Day/a.jpg"]
    assert unlisted == []


@pytest.mark.fast
@pytest.mark.parametrize("junk", UNCHECKABLE, indirect=True)
def test_a_junk_file_that_cannot_be_checked_doesnt_block_deletions(junk: Path) -> None:
    server = FakeScans(default=("unchanged", None, "ast_a"), gone=1)
    stats = _scan(server, junk, library_filters=EXPORTS)
    assert server.unlisted() == []
    assert [f["rel_path"] for f in server.found()] == ["Day/a.jpg"]
    assert stats.deleted == 1 and stats.unlisted == []


@pytest.mark.fast
def test_a_folder_whose_name_the_server_refuses_holds_back_the_whole_library(tmp_path: Path) -> None:
    # It can't be named to the server, so nothing in the library is taken for gone.
    root, local = _library(tmp_path, on_disk=1)

    def walk(*args, unlisted, **kwargs):
        unlisted.append("back\\slash")
        return local

    server = FakeScans(default=("unchanged", None, "ast_1"))
    _scan(server, root, walk=walk)
    assert server.unlisted() == [""]
    assert server.sent("POST", f"/v1/scans/{SCAN_ID}/plan")
