"""Scanning is safe from a network mount (ADR-016 phase 0).

- rel_path is Unicode NFC, as the macOS scanner stores it, so the same file
  has one identity whichever machine scans it.
- Source files are found on disk even when their names are stored in
  another normalization form (macOS-created NFD names on a Linux mount).
- A scan that would delete a large share of the library skips deletions,
  as the macOS scanner does: the root is probably half-mounted.
"""

from __future__ import annotations

import errno
import os
import unicodedata
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.processing.ingest import _walk_library
from src.processing.scan import _ServerAsset, run_scan
from src.shared.io_utils import resolve_source_path
from src.shared.path_filter import PathFilter

NFC = unicodedata.normalize("NFC", "Café/résumé.jpg")
NFD = unicodedata.normalize("NFD", NFC)


def _write(root: Path, rel: str, data: bytes = b"jpeg") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.fast
def test_walk_library_stores_nfc_rel_path(tmp_path: Path) -> None:
    _write(tmp_path, NFD)

    [entry] = _walk_library(tmp_path)

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


@pytest.mark.fast
def _scan_with(
    tmp_path: Path,
    *,
    on_disk: int,
    on_server: int,
    extra_server: dict | None = None,
    ignored: set[str] | None = None,
    split: MagicMock | None = None,
    local: list[dict] | None = None,
    walk=None,
    settings: dict | str | None = None,
    raw_answers: list | None = None,
    **kwargs,
) -> MagicMock:
    """run_scan with `on_server` images on the server, of which `on_disk` are on disk unchanged."""
    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    (root / "f0.jpg").touch()  # a library on a mounted share has something in it
    if local is None:
        local = [
            {"rel_path": f"f{i}.jpg", "file_size": 4, "file_mtime": None, "media_type": "image", "ext": ".jpg"}
            for i in range(on_disk)
        ]
    existing = {
        f"f{i}.jpg": _ServerAsset(asset_id=f"ast_{i}", sha256="x", file_size=4, media_type="image")
        for i in range(on_server)
    }
    existing.update(extra_server or {})
    client = MagicMock()
    client.raw.return_value = _answer(200, {"trashed": [], "not_found": [], "handed_over": []})
    if raw_answers is not None:  # the archive requests answer in turn
        client.raw.side_effect = raw_answers
    if settings == "unreadable":
        client.get.side_effect = RuntimeError("the server didn't answer")
    elif settings is not None:
        client.get.return_value.json.return_value = settings
    with (
        patch("src.processing.scan._load_tenant_filters", return_value=[]),
        patch("src.processing.scan._load_library_filters", return_value=[]),
        patch("src.processing.scan._walk_library", side_effect=walk or (lambda *a, **k: local)),
        patch("src.processing.scan._fetch_existing_assets_with_sha", return_value=existing),
        patch("src.processing.scan._fetch_ignored_paths", return_value=ignored or {}),
        patch("src.processing.scan._split_files", split or MagicMock(return_value=([], [], local))),
        patch("src.processing.scan._populate_cache_for_unchanged"),
    ):
        client.scan_stats = run_scan(
            client,
            {"library_id": "lib_1", "root_path": str(root)},
            console=Console(quiet=True),
            skip_moves=kwargs.pop("skip_moves", False),
            allow_moves=True,
            **kwargs,
        )
    return client


def _answer(status: int, body: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = str(body)
    return r


def _archive_calls(client: MagicMock) -> list:
    return [c for c in client.raw.call_args_list if c.args == ("DELETE", "/v1/assets")]


def _deleted_ids(client: MagicMock) -> list[str]:
    """The clips the scan archived as missing: the last request's (a 409 asked first)."""
    calls = _archive_calls(client)
    if not calls:
        return []
    body = calls[-1].kwargs["json"]
    # The scanner's deletions are "missing", never the user's trash.
    assert body["reason"] == "missing"
    return list(body["asset_ids"])


@pytest.mark.fast
def test_a_mount_gone_when_files_look_missing_stops_the_scan(tmp_path: Path) -> None:
    """Files that look missing make the scan check the mount again; if it has
    gone (a network drop, a clean unmount mid-walk), the scan stops and
    archives nothing (Robert's rule, Oct 8). The worker keeps the changes."""
    root = tmp_path / "lib"
    with patch("src.processing.scan.reachable_root", side_effect=[root, None]) as probe:
        client = _scan_with(tmp_path, on_disk=10, on_server=100, allow_mass_delete=True)

    assert _deleted_ids(client) == []
    assert client.scan_stats.root_unreachable
    assert probe.call_args_list[1].kwargs == {"require_entries": True}


@pytest.mark.fast
def test_files_missing_from_a_mount_still_there_are_archived(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    with patch("src.processing.scan.reachable_root", side_effect=[root, root]):
        client = _scan_with(tmp_path, on_disk=10, on_server=100, allow_mass_delete=True)

    assert len(_deleted_ids(client)) == 90
    assert not client.scan_stats.root_unreachable


@pytest.mark.fast
def test_a_scan_with_nothing_missing_doesnt_check_the_mount_again(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    with patch("src.processing.scan.reachable_root", return_value=root) as probe:
        _scan_with(tmp_path, on_disk=100, on_server=100)

    assert probe.call_count == 1


@pytest.mark.fast
def test_an_unmounted_share_mid_scan_archives_nothing(tmp_path: Path, monkeypatch) -> None:
    """End to end through the real mount check: fstab lists the share, it's
    mounted when the scan starts, and it goes away during the walk."""
    from src.processing import roots

    root = tmp_path / "lib"
    fstab = tmp_path / "fstab"
    fstab.write_text(f"//nas/share {root} cifs soft 0 0\n")
    monkeypatch.setattr(roots, "FSTAB", fstab)
    mounted = {str(root)}
    monkeypatch.setattr(roots.os.path, "ismount", lambda p: str(p) in mounted)
    local = [{"rel_path": f"f{i}.jpg", "file_size": 4, "file_mtime": None, "media_type": "image", "ext": ".jpg"}
             for i in range(10)]

    def walk(*args, **kwargs):
        mounted.clear()  # unmounted mid-walk: the files not yet seen look missing
        return local

    client = _scan_with(tmp_path, on_disk=10, on_server=100, allow_mass_delete=True, local=local, walk=walk)

    assert _deleted_ids(client) == []
    assert client.scan_stats.root_unreachable


@pytest.mark.fast
def test_a_share_gone_before_the_walk_found_anything_keeps_the_changes(tmp_path: Path) -> None:
    """An empty walk on a library that had files: the storage is checked
    again, and if it's gone the scan says so (the worker then keeps the
    change reports for when it's back)."""
    root = tmp_path / "lib"
    with patch("src.processing.scan.reachable_root", side_effect=[root, None]):
        client = _scan_with(tmp_path, on_disk=0, on_server=100, allow_mass_delete=True)
    assert _deleted_ids(client) == []
    assert client.scan_stats.root_unreachable


@pytest.mark.fast
def test_a_library_with_no_media_on_a_present_share_is_just_empty(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    with patch("src.processing.scan.reachable_root", side_effect=[root, root]):
        client = _scan_with(tmp_path, on_disk=0, on_server=0)
    assert not client.scan_stats.root_unreachable


def _moved_file_scan(tmp_path: Path, *, follow_moves: bool, **kwargs) -> tuple[MagicMock, MagicMock, MagicMock]:
    """f1.jpg moved to moved/f1.jpg: on the server as f0 and f1, on disk as f0 and the new path."""
    moved = {"rel_path": "moved/f1.jpg", "file_size": 4, "file_mtime": None, "media_type": "image", "ext": ".jpg"}
    local = [{"rel_path": "f0.jpg", "file_size": 4, "file_mtime": None, "media_type": "image", "ext": ".jpg"}, moved]
    with (
        patch("src.processing.scan._detect_moves", return_value=([], [moved])) as detect,
        patch("src.processing.scan._scan_one") as scan_one,
        patch("src.processing.scan.ProxyCache"),
    ):
        client = _scan_with(tmp_path, on_disk=1, on_server=2, local=local,
                            split=MagicMock(return_value=([moved], [], [local[0]])),
                            settings={"follow_moves": follow_moves}, **kwargs)
    return client, detect, scan_one


@pytest.mark.fast
def test_moves_off_a_moved_file_is_archived_and_scanned_as_new(tmp_path: Path) -> None:
    """The account doesn't follow moves (Robert's call, Oct 8): the path is the identity."""
    client, detect, scan_one = _moved_file_scan(tmp_path, follow_moves=False)
    detect.assert_not_called()
    assert _deleted_ids(client) == ["ast_1"]
    assert [c.kwargs["f"]["rel_path"] for c in scan_one.call_args_list] == ["moved/f1.jpg"]
    assert not [c for c in client.post.call_args_list if c.args and c.args[0] == "/v1/assets/batch-moves"]


@pytest.mark.fast
def test_moves_off_skip_moves_doesnt_hold_back_deletions(tmp_path: Path) -> None:
    # --skip-moves skips deletions only because a delete might be half a move.
    client, _, _ = _moved_file_scan(tmp_path, follow_moves=False, skip_moves=True)
    assert _deleted_ids(client) == ["ast_1"]


@pytest.mark.fast
def test_settings_it_cant_read_mean_no_move_detection(tmp_path: Path) -> None:
    """Unsure whether the account follows moves, the scan looks for none: the
    old path is archived and the new one ingested, and a server that follows
    moves restores the asset there by content anyway."""
    client, detect, scan_one = _moved_file_scan_unreadable(tmp_path)
    detect.assert_not_called()
    assert _deleted_ids(client) == ["ast_1"]
    assert [c.kwargs["f"]["rel_path"] for c in scan_one.call_args_list] == ["moved/f1.jpg"]


def _moved_file_scan_unreadable(tmp_path: Path):
    moved = {"rel_path": "moved/f1.jpg", "file_size": 4, "file_mtime": None, "media_type": "image", "ext": ".jpg"}
    local = [{"rel_path": "f0.jpg", "file_size": 4, "file_mtime": None, "media_type": "image", "ext": ".jpg"}, moved]
    with (
        patch("src.processing.scan._detect_moves", return_value=([], [moved])) as detect,
        patch("src.processing.scan._scan_one") as scan_one,
        patch("src.processing.scan.ProxyCache"),
    ):
        client = _scan_with(tmp_path, on_disk=1, on_server=2, local=local,
                            split=MagicMock(return_value=([moved], [], [local[0]])), settings="unreadable")
    return client, detect, scan_one


@pytest.mark.fast
def test_settings_it_cant_read_still_let_skip_moves_hold_back_deletions(tmp_path: Path) -> None:
    """Unsure whether moves are followed, --skip-moves keeps its promise: a
    deletion might be half of a move, so none are sent."""
    moved = {"rel_path": "moved/f1.jpg", "file_size": 4, "file_mtime": None, "media_type": "image", "ext": ".jpg"}
    local = [{"rel_path": "f0.jpg", "file_size": 4, "file_mtime": None, "media_type": "image", "ext": ".jpg"}, moved]
    with patch("src.processing.scan._scan_one"), patch("src.processing.scan.ProxyCache"):
        client = _scan_with(tmp_path, on_disk=1, on_server=2, local=local,
                            split=MagicMock(return_value=([moved], [], [local[0]])), settings="unreadable",
                            skip_moves=True)
    assert _deleted_ids(client) == []


@pytest.mark.fast
def test_an_archive_that_moved_to_a_copy_counts_as_moved(tmp_path: Path) -> None:
    """The server moved one of the two to an empty copy of its file (copy, then delete)."""
    client = MagicMock()
    client.raw.return_value = _answer(200, {"trashed": ["ast_1", "ast_2"], "not_found": [],
                                            "handed_over": ["ast_2"]})
    stats = _scan_counting(tmp_path, client)
    assert (stats.deleted, stats.moved) == (1, 1)


def _scan_counting(tmp_path: Path, client: MagicMock):
    from src.processing.scan import run_scan

    root = tmp_path / "lib"
    root.mkdir(exist_ok=True)
    (root / "f0.jpg").touch()
    local = [{"rel_path": "f0.jpg", "file_size": 4, "file_mtime": None, "media_type": "image", "ext": ".jpg"}]
    existing = {f"f{i}.jpg": _ServerAsset(asset_id=f"ast_{i}", sha256="x", file_size=4, media_type="image")
                for i in range(3)}
    client.get.return_value.json.return_value = {"follow_moves": True}
    with (
        patch("src.processing.scan._load_tenant_filters", return_value=[]),
        patch("src.processing.scan._load_library_filters", return_value=[]),
        patch("src.processing.scan._walk_library", return_value=local),
        patch("src.processing.scan._fetch_existing_assets_with_sha", return_value=existing),
        patch("src.processing.scan._fetch_ignored_paths", return_value={}),
        patch("src.processing.scan._split_files", MagicMock(return_value=([], [], local))),
        patch("src.processing.scan._populate_cache_for_unchanged"),
    ):
        return run_scan(client, {"library_id": "lib_1", "root_path": str(root)}, console=Console(quiet=True),
                        allow_moves=True, allow_mass_delete=True)


@pytest.mark.fast
def test_moves_on_moves_are_looked_for(tmp_path: Path) -> None:
    _, detect, _ = _moved_file_scan(tmp_path, follow_moves=True)
    detect.assert_called_once()


@pytest.mark.fast
def test_scan_mass_deletion_can_be_allowed(tmp_path: Path) -> None:
    client = _scan_with(tmp_path, on_disk=10, on_server=100, allow_mass_delete=True)

    assert len(_deleted_ids(client)) == 90


@pytest.mark.fast
def test_scan_small_deletion_proceeds(tmp_path: Path) -> None:
    client = _scan_with(tmp_path, on_disk=95, on_server=100)

    assert len(_deleted_ids(client)) == 5


@pytest.mark.fast
def test_media_type_scan_never_deletes_other_types(tmp_path: Path) -> None:
    """`scan --media-type image` doesn't see videos on disk; it must not
    treat the library's videos as gone."""
    videos = {
        f"v{i}.mov": _ServerAsset(asset_id=f"vid_{i}", sha256="x", file_size=4, media_type="video")
        for i in range(30)
    }

    client = _scan_with(tmp_path, on_disk=10, on_server=10, extra_server=videos, media_type_filter="image")

    assert _deleted_ids(client) == []


@pytest.mark.fast
def test_scan_skips_trashed_paths(tmp_path: Path) -> None:
    split = MagicMock(return_value=([], [], []))

    _scan_with(tmp_path, on_disk=5, on_server=5, ignored={"f0.jpg": None, "f3.jpg": None}, split=split)

    scanned = {f["rel_path"] for f in split.call_args[0][0]}
    assert scanned == {"f1.jpg", "f2.jpg", "f4.jpg"}


@pytest.mark.fast
def test_another_file_at_a_trashed_path_is_scanned(tmp_path: Path) -> None:
    # A changed file is a new clip: only the file a person removed is skipped.
    from src.processing.scan import _ServerAsset

    split = MagicMock(return_value=([], [], []))
    removed = [_ServerAsset(asset_id="", sha256="a" * 64)]
    with patch("src.processing.scan.compute_sha256",
               side_effect=lambda p: "a" * 64 if p.name == "f1.jpg" else "b" * 64) as hashed:
        _scan_with(tmp_path, on_disk=4, on_server=4, ignored={"f1.jpg": removed, "f2.jpg": removed}, split=split)

    scanned = {f["rel_path"] for f in split.call_args[0][0]}
    assert scanned == {"f0.jpg", "f2.jpg", "f3.jpg"}
    assert {c.args[0].name for c in hashed.call_args_list} == {"f1.jpg", "f2.jpg"}  # only those are hashed


@pytest.mark.fast
def test_a_removed_file_left_as_it_was_isnt_read_again(tmp_path: Path) -> None:
    # Review round 2: archives kept on disk were hashed on every scan.
    from datetime import datetime, timezone

    from src.processing.scan import _ServerAsset

    when = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    local = [{"rel_path": f"f{i}.jpg", "file_size": 4, "file_mtime": when, "media_type": "image", "ext": ".jpg"}
             for i in range(3)]
    removed = [_ServerAsset(asset_id="", sha256="a" * 64, file_size=4, file_mtime="2026-10-01T08:00:00-04:00")]
    split = MagicMock(return_value=([], [], []))
    with patch("src.processing.scan.compute_sha256", return_value="b" * 64) as hashed:
        _scan_with(tmp_path, on_disk=3, on_server=3, local=local, ignored={"f1.jpg": removed}, split=split)
    assert hashed.call_count == 0
    assert {f["rel_path"] for f in split.call_args[0][0]} == {"f0.jpg", "f2.jpg"}


@pytest.mark.fast
def test_path_prefix_is_nfc() -> None:
    from src.shared.io_utils import normalize_path_prefix

    assert normalize_path_prefix("/" + unicodedata.normalize("NFD", "Café/") ) == unicodedata.normalize("NFC", "Café")


# ---------------------------------------------------------------------------
# Files still being written (ADR-016 phase 2): the brain scans as soon as the
# Mac reports a copy, so a file still growing waits for a later scan, as the
# macOS scanner's 30-second quarantine does.
# ---------------------------------------------------------------------------


def _local(rel: str, age_sec: float) -> dict:
    from datetime import datetime, timedelta, timezone

    return {"rel_path": rel, "file_size": 4, "media_type": "image", "ext": ".jpg",
            "file_mtime": datetime.now(timezone.utc) - timedelta(seconds=age_sec)}


@pytest.mark.fast
def test_files_still_being_written_wait_for_a_later_scan(tmp_path: Path) -> None:
    local = [_local("done.jpg", 600), _local("copying.jpg", 2)]
    split = MagicMock(return_value=([], [], []))
    _scan_with(tmp_path, on_disk=0, on_server=0, local=local, split=split)
    passed = [f["rel_path"] for f in split.call_args.args[0]]
    assert passed == ["done.jpg"]


@pytest.mark.fast
def test_a_file_being_rewritten_is_not_deleted(tmp_path: Path) -> None:
    # It's on the server and on disk, just changing: never "missing".
    local = [_local(f"f{i}.jpg", 600) for i in range(9)] + [_local("f9.jpg", 1)]
    client = _scan_with(tmp_path, on_disk=0, on_server=10, local=local,
                        split=MagicMock(return_value=([], [], [])))
    assert _deleted_ids(client) == []


@pytest.mark.fast
def test_scan_counts_files_still_being_written(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    local = [_local("done.jpg", 600), _local("copying.jpg", 2)]
    with (
        patch("src.processing.scan._load_tenant_filters", return_value=[]),
        patch("src.processing.scan._load_library_filters", return_value=[]),
        patch("src.processing.scan._walk_library", return_value=local),
        patch("src.processing.scan._fetch_existing_assets_with_sha", return_value={}),
        patch("src.processing.scan._fetch_ignored_paths", return_value={}),
        patch("src.processing.scan._split_files", MagicMock(return_value=([], [], []))),
        patch("src.processing.scan._populate_cache_for_unchanged"),
    ):
        stats = run_scan(MagicMock(), {"library_id": "lib_1", "root_path": str(root)},
                         console=Console(quiet=True), skip_moves=True)
    assert stats.settling == 1
    assert stats.settling_paths == ["copying.jpg"]


@pytest.mark.fast
def test_a_camera_clock_far_ahead_is_not_taken_for_a_copy(tmp_path: Path) -> None:
    # Stamped an hour in the future, it would otherwise wait an hour.
    local = [_local("ahead.jpg", -3600), _local("copying.jpg", -60)]
    split = MagicMock(return_value=([], [], []))
    _scan_with(tmp_path, on_disk=0, on_server=0, local=local, split=split)
    passed = [f["rel_path"] for f in split.call_args.args[0]]
    assert passed == ["ahead.jpg"]


@pytest.mark.fast
def test_scan_names_the_files_that_failed(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    (root / "bad.jpg").write_bytes(b"jpeg")
    new = [{"rel_path": "bad.jpg", "file_size": 4, "file_mtime": None, "media_type": "image", "ext": ".jpg"}]
    with (
        patch("src.processing.scan._load_tenant_filters", return_value=[]),
        patch("src.processing.scan._load_library_filters", return_value=[]),
        patch("src.processing.scan._walk_library", return_value=new),
        patch("src.processing.scan._fetch_existing_assets_with_sha", return_value={}),
        patch("src.processing.scan._fetch_ignored_paths", return_value={}),
        patch("src.processing.scan._generate_proxy_bytes", side_effect=RuntimeError("not a JPEG")),
        patch("src.processing.scan.ProxyCache"),
        patch("src.processing.scan._populate_cache_for_unchanged"),
    ):
        stats = run_scan(MagicMock(), {"library_id": "lib_1", "root_path": str(root)},
                         console=Console(quiet=True), skip_moves=True)
    assert stats.failed == 1
    assert stats.failed_paths == ["bad.jpg"]


# ---------------------------------------------------------------------------
# A scan sees the folder it was given (ADR-016 phase 2): the worker
# acknowledges reports after the scan, so a walk that quietly finds nothing
# would drop them.
# ---------------------------------------------------------------------------

ZURICH = unicodedata.normalize("NFC", "Zürich")


def _scan_disk(root: Path, existing: dict[str, _ServerAsset], *, library_filters: list | None = None,
               **kwargs) -> MagicMock:
    """run_scan over real files under root, with every file on disk unchanged."""
    client = MagicMock()
    client.raw.return_value = _answer(200, {"trashed": [], "not_found": [], "handed_over": []})
    with (
        patch("src.processing.scan._load_tenant_filters", return_value=[]),
        patch("src.processing.scan._load_library_filters", return_value=library_filters or []),
        patch("src.processing.scan._fetch_existing_assets_with_sha", return_value=existing),
        patch("src.processing.scan._fetch_ignored_paths", return_value={}),
        patch("src.processing.scan._split_files", side_effect=lambda files, existing, thorough: ([], [], files)),
        patch("src.processing.scan.ProxyCache"),
        patch("src.processing.scan._populate_cache_for_unchanged"),
    ):
        client.stats = run_scan(client, {"library_id": "lib_1", "root_path": str(root)},
                                console=Console(quiet=True), allow_moves=True, **kwargs)
    return client


def _assets(*rel_paths: str) -> dict[str, _ServerAsset]:
    return {rel: _ServerAsset(asset_id=f"ast_{rel}", sha256="x", file_size=4, media_type="image")
            for rel in rel_paths}


@pytest.mark.fast
def test_walk_finds_a_folder_named_in_nfd(tmp_path: Path) -> None:
    _write(tmp_path, unicodedata.normalize("NFD", ZURICH) + "/A001.jpg")
    [entry] = _walk_library(tmp_path, ZURICH)
    assert entry["rel_path"] == f"{ZURICH}/A001.jpg"


@pytest.mark.fast
def test_scanning_a_folder_named_in_nfd_keeps_its_files(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    _write(root, unicodedata.normalize("NFD", ZURICH) + "/A001.jpg")
    client = _scan_disk(root, _assets(f"{ZURICH}/A001.jpg", f"{ZURICH}/gone.jpg"), path_prefix=ZURICH)
    assert _deleted_ids(client) == [f"ast_{ZURICH}/gone.jpg"]


@pytest.mark.fast
def test_a_deleted_folder_is_seen_from_its_parent(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    _write(root, "Shoots/keep.jpg")
    client = _scan_disk(root, _assets("Shoots/keep.jpg", "Shoots/Gone/a.jpg", "Other/b.jpg"),
                        path_prefix="Shoots/Gone")
    assert _deleted_ids(client) == ["ast_Shoots/Gone/a.jpg"]


@pytest.mark.fast
def test_an_emptied_folder_sees_its_deletions(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    (root / "Shoots" / "Day 1").mkdir(parents=True)
    _write(root, "Other/b.jpg")
    client = _scan_disk(root, _assets("Shoots/Day 1/a.jpg", "Other/b.jpg"), path_prefix="Shoots/Day 1")
    assert _deleted_ids(client) == ["ast_Shoots/Day 1/a.jpg"]


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
    assert [f["rel_path"] for f in _walk_library(root)] == [".h.jpg", ".hid/g.jpg", "a/f.jpg"]


@pytest.mark.fast
def test_walk_of_a_symlinked_prefix_lists_nothing_and_deletes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    _write(root, "a/f.jpg")
    _write(tmp_path, "other/o.jpg")
    (root / "linked").symlink_to(tmp_path / "other")
    unlisted: list[str] = []
    assert _walk_library(root, "linked", unlisted=unlisted) == []
    assert unlisted == ["linked"]


# ---------------------------------------------------------------------------
# A folder that can't be listed (no permission, or a CIFS soft mount timing
# out mid-walk) must not look deleted.
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
    files = _walk_library(locked, unlisted=unlisted)
    assert [f["rel_path"] for f in files] == ["open/a.jpg"]
    assert unlisted == ["Locked"]


@pytest.mark.fast
@needs_permissions
def test_a_folder_that_cannot_be_listed_deletes_nothing(locked: Path) -> None:
    client = _scan_disk(locked, _assets("open/a.jpg", "Locked/b.jpg", "gone.jpg"))
    assert _deleted_ids(client) == []


@pytest.mark.fast
@needs_permissions
def test_a_scan_says_which_folders_it_could_not_list(locked: Path) -> None:
    with (
        patch("src.processing.scan._load_tenant_filters", return_value=[]),
        patch("src.processing.scan._load_library_filters", return_value=[]),
        patch("src.processing.scan._fetch_existing_assets_with_sha", return_value=_assets("open/a.jpg")),
        patch("src.processing.scan._fetch_ignored_paths", return_value={}),
        patch("src.processing.scan._split_files", side_effect=lambda files, existing, thorough: ([], [], files)),
        patch("src.processing.scan.ProxyCache"),
        patch("src.processing.scan._populate_cache_for_unchanged"),
    ):
        stats = run_scan(MagicMock(), {"library_id": "lib_1", "root_path": str(locked)}, console=Console(quiet=True))
    assert stats.unlisted == ["Locked"]


@pytest.mark.fast
@needs_permissions
def test_moves_outside_a_folder_that_cannot_be_listed_still_apply(locked: Path) -> None:
    from src.processing.workers.exif_extract import compute_sha256

    moved = _write(locked, "open/moved.jpg", b"moved bytes")
    for f in (moved, locked / "open" / "a.jpg"):
        os.utime(f, (1e9, 1e9))  # long settled
    sha = compute_sha256(moved)
    existing = {
        "Locked/b.jpg": _ServerAsset(asset_id="ast_b", sha256=sha, file_size=11, media_type="image"),
        "was/moved.jpg": _ServerAsset(asset_id="ast_moved", sha256=sha, file_size=11, media_type="image"),
    }
    client = MagicMock()
    with (
        patch("src.processing.scan._load_tenant_filters", return_value=[]),
        patch("src.processing.scan._load_library_filters", return_value=[]),
        patch("src.processing.scan._fetch_existing_assets_with_sha", return_value=existing),
        patch("src.processing.scan._fetch_ignored_paths", return_value={}),
        patch("src.processing.scan._scan_one"),
        patch("src.processing.scan.ProxyCache"),
        patch("src.processing.scan._populate_cache_for_unchanged"),
    ):
        run_scan(client, {"library_id": "lib_1", "root_path": str(locked)}, path_prefix=None,
                 console=Console(quiet=True), allow_moves=True)
    moves = [c.kwargs["json"]["items"] for c in client.post.call_args_list if c.args[0] == "/v1/assets/batch-moves"]
    # Never the asset in the folder that couldn't be listed: it may still be there.
    assert moves == [[{"asset_id": "ast_moved", "rel_path": "open/moved.jpg"}]]
    assert _deleted_ids(client) == []


@pytest.mark.fast
def test_a_root_gone_mid_scan_is_reported_unreachable(tmp_path: Path) -> None:
    # Checked reachable, then unmounted: nothing was scanned, so the worker
    # must keep the reports.
    with (
        patch("src.processing.scan.reachable_root", return_value=tmp_path / "gone"),
        patch("src.processing.scan._load_tenant_filters", return_value=[]),
        patch("src.processing.scan._load_library_filters", return_value=[]),
    ):
        stats = run_scan(MagicMock(), {"library_id": "lib_1", "root_path": "/x"}, path_prefix="Day 1",
                         console=Console(quiet=True))
    assert stats.root_unreachable


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
    files = _walk_library(flaky, unlisted=unlisted)
    assert [f["rel_path"] for f in files] == ["open/a.jpg"]
    assert unlisted == ["Flaky"]


@pytest.mark.fast
@pytest.mark.parametrize("flaky", UNCHECKABLE, indirect=True)
def test_files_that_cannot_be_checked_are_not_deleted(flaky: Path) -> None:
    client = _scan_disk(flaky, _assets("Flaky/b.jpg", "Flaky/c.jpg"), path_prefix="Flaky")
    assert _deleted_ids(client) == []
    assert client.stats.unlisted == ["Flaky"]


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
    assert [f["rel_path"] for f in _walk_library(root, unlisted=unlisted)] == ["Day/a.jpg"]
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
    client = _scan_disk(root, _assets("Shoots/Day 1/a.jpg", "Shoots/Day 1/b.jpg"), path_prefix="Shoots/Day 1")
    assert _deleted_ids(client) == []
    assert client.stats.unlisted == ["Shoots/Day 1"]


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
    files = _walk_library(junk, library_filters=EXPORTS, unlisted=unlisted)
    assert [f["rel_path"] for f in files] == ["Day/a.jpg"]
    assert unlisted == []


@pytest.mark.fast
@pytest.mark.parametrize("junk", UNCHECKABLE, indirect=True)
def test_a_junk_file_that_cannot_be_checked_doesnt_block_deletions(junk: Path) -> None:
    client = _scan_disk(junk, _assets("Day/a.jpg", "Day/gone.jpg"), library_filters=EXPORTS)
    assert _deleted_ids(client) == ["ast_Day/gone.jpg"]
    assert client.stats.unlisted == []


def _mass_missing() -> MagicMock:
    return _answer(409, {"error": {"code": "mass_missing", "message": "90 clips would be archived as missing.",
                                   "details": {"count": 90}}})


@pytest.mark.fast
def test_a_scan_sends_its_missing_clips_in_one_request(tmp_path: Path) -> None:
    """The server's mass-missing rule judges a scan's missing clips together."""
    root = tmp_path / "lib"
    with patch("src.processing.scan.reachable_root", side_effect=[root, root]):
        client = _scan_with(tmp_path, on_disk=10, on_server=1200)
    calls = _archive_calls(client)
    assert len(calls) == 1 and len(calls[0].kwargs["json"]["asset_ids"]) == 1190
    assert "confirm_missing" not in calls[0].kwargs["json"]


@pytest.mark.fast
def test_the_server_asking_about_mass_missing_skips_them(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    with patch("src.processing.scan.reachable_root", side_effect=[root, root]):
        client = _scan_with(tmp_path, on_disk=10, on_server=100, raw_answers=[_mass_missing()])
    assert len(_archive_calls(client)) == 1
    assert client.scan_stats.deleted == 0


@pytest.mark.fast
def test_allow_mass_delete_answers_with_the_count(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    ok = _answer(200, {"trashed": [], "not_found": [], "handed_over": []})
    with patch("src.processing.scan.reachable_root", side_effect=[root, root]):
        client = _scan_with(tmp_path, on_disk=10, on_server=100, allow_mass_delete=True,
                            raw_answers=[_mass_missing(), ok])
    calls = _archive_calls(client)
    assert len(calls) == 2
    assert calls[1].kwargs["json"]["confirm_missing"] == 90
    assert client.scan_stats.deleted == 90
