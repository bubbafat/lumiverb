"""run_scan, the client's side of a scan (ADR-018 decision 12).

The client says what it found and the server decides. These tests say what
run_scan sends and what it does with each answer, over a scripted server
(tests/scan_fakes.py). What the server decides is tested on the real one:
tests/test_scans_api.py rule by rule, tests/test_scan_outcomes.py end to end.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from rich.console import Console

from src.processing.originals import Folder
from src.processing.scan import (
    Previews,
    ScanStats,
    _hashed,
    _populate_cache_for_unchanged,
    run_scan,
)
from tests.scan_fakes import SCAN_ID, FakeScans, answer, finished, mass_missing

pytestmark = pytest.mark.fast

HOUR_AGO = time.time() - 3600


@pytest.fixture(autouse=True)
def _cache_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan's proxy cache in the test's own folder, never ~/.cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "lib"
    path.mkdir()
    return path


def _photo(root: Path, rel: str, *, mtime: float = HOUR_AGO, shade: int = 0) -> Path:
    """A real JPEG (the scan makes its preview), settled unless said otherwise."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), (shade % 256, 80, 40)).save(path, format="JPEG")
    os.utime(path, (mtime, mtime))
    return path


def _file(root: Path, rel: str, data: bytes = b"bytes", *, mtime: float = HOUR_AGO) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def _scan(server: FakeScans, root: Path, **kwargs) -> ScanStats:
    kwargs = {"concurrency": 1, "console": Console(quiet=True), **kwargs}
    with (
        patch("src.processing.scan._told", return_value=([], [], Previews())),
    ):
        return run_scan(server.client(), {"library_id": "lib_1", "root_path": str(root)}, **kwargs)


def _sha(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


# ---------------------------------------------------------------------------
# A client's scan keeps nothing (ADR-020 decision 7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("planned", [("upload", "new", None), ("unchanged", None, "ast_x")])
def test_a_client_s_scan_keeps_no_previews(root: Path, tmp_path: Path, planned: tuple) -> None:
    """`lumiverb scan` (run_scan as the CLI calls it) uploads what it makes
    and keeps no copy, nor downloads the unchanged clips' previews: it's a
    client of the API, which has them."""
    _photo(root, "a.jpg")
    server = FakeScans(default=planned)
    _scan(server, root)
    assert not (tmp_path / "cache" / "lumiverb" / "proxies").exists()
    assert not any(c.startswith("GET /v1/assets/") for c in server.calls())


def test_the_scheduler_apart_from_the_api_keeps_its_scans_previews(root: Path, tmp_path: Path) -> None:
    """Its producers read them there (cache_previews, as scheduler/scans.py
    asks when the API isn't on this machine)."""
    _photo(root, "a.jpg")
    _scan(FakeScans(), root, cache_previews=True)
    kept = tmp_path / "cache" / "lumiverb" / "proxies"
    assert sorted(p.suffix for p in kept.iterdir() if not p.name.startswith(".")) == ["", ".sha"]


# ---------------------------------------------------------------------------
# What's sent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("options", "moves"), [
    ({"allow_moves": True}, "apply"),  # the scheduler's
    ({"skip_moves": True}, "skip"),
    ({}, "ask"),  # the CLI's prompt
])
def test_a_scan_starts_before_the_walk_with_its_options(root: Path, options: dict, moves: str) -> None:
    _photo(root, "a.jpg")
    server = FakeScans()
    started_before: list[bool] = []
    from src.processing.originals import Folder

    real_walk = Folder.walk

    def walk(self, *args, **kwargs):
        started_before.append(any(r.path == "/v1/libraries/lib_1/scans" for r in server.requests))
        return real_walk(self, *args, **kwargs)

    with patch.object(Folder, "walk", walk):
        _scan(server, root, thorough=True, **options)

    assert started_before == [True]  # a clip made after the start is never taken for gone
    assert server.started() == {"scope": "", "media_type": "all", "thorough": True, "force": False,
                                "moves": moves, "dry_run": False, "force_add": False}
    assert server.calls() == ["POST /v1/libraries/lib_1/scans", f"POST /v1/scans/{SCAN_ID}/found",
                              f"POST /v1/scans/{SCAN_ID}/plan", f"POST /v1/scans/{SCAN_ID}/finish", "POST /v1/ingest"]


def test_every_media_file_is_listed_with_its_size_and_time(root: Path) -> None:
    from datetime import UTC, datetime

    a = _file(root, "Day 1/a.jpg", b"12345")
    _file(root, "Day 1/clip.mov", b"123")
    _file(root, "empty.jpg", b"")
    _file(root, "notes.txt", b"not media")
    server = FakeScans(default=("unchanged", None, "ast_x"))

    stats = _scan(server, root, media_type_filter="image")

    when = datetime.fromtimestamp(a.stat().st_mtime, tz=UTC).isoformat()
    # Every media file in the scope, of every media type, the empty one too:
    # none of them is gone. The server plans only the scan's media type.
    assert server.found() == [
        {"rel_path": "Day 1/a.jpg", "file_size": 5, "file_mtime": when, "settling": False},
        {"rel_path": "Day 1/clip.mov", "file_size": 3, "file_mtime": when, "settling": False},
        {"rel_path": "empty.jpg", "file_size": 0, "file_mtime": when, "settling": False},
    ]
    assert server.started()["media_type"] == "image"
    assert stats.unchanged == 1


def test_a_long_listing_goes_a_thousand_files_a_call_and_the_whole_plan_is_read(root: Path) -> None:
    for n in range(2500):
        _file(root, f"f{n:04}.jpg", b"x")
    server = FakeScans(default=("unchanged", None, "ast_x"))

    stats = _scan(server, root, cache_previews=True)  # the unchanged listed too: the scheduler apart from the API

    assert [len(r.body["files"]) for r in server.sent("POST", f"/v1/scans/{SCAN_ID}/found")] == [1000, 1000, 500]
    pages = server.sent("GET", f"/v1/scans/{SCAN_ID}/plan")
    assert len(pages) == 5  # 500 with the plan, 4 pages of 500, then an empty one: there's no more
    assert all(p.params["action"] == "upload,hash,skipped,unchanged" for p in pages)
    assert stats.unchanged == 2500


def test_a_folder_scan_says_its_folder(root: Path) -> None:
    _photo(root, "Shoots/Day 1/a.jpg")
    server = FakeScans()
    _scan(server, root, path_prefix="Shoots/Day 1/")
    assert server.started()["scope"] == "Shoots/Day 1"
    assert [f["rel_path"] for f in server.found()] == ["Shoots/Day 1/a.jpg"]


def test_a_name_the_server_refuses_is_never_sent(root: Path, caplog: pytest.LogCaptureFixture) -> None:
    # A backslash is a name on a Mac or Linux, never a library path: the
    # whole listing would be refused. Skipped, never a failure to retry.
    import logging

    _photo(root, "keep.jpg")
    _photo(root, "back\\slash.jpg")
    server = FakeScans()
    with caplog.at_level(logging.WARNING, logger="src.processing.scan"):
        stats = _scan(server, root)
    assert [f["rel_path"] for f in server.found()] == ["keep.jpg"]
    assert (stats.new, stats.failed, stats.failed_paths) == (1, 0, [])
    assert any("refuses its name" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Hashing what the plan asks about
# ---------------------------------------------------------------------------


def test_only_what_the_plan_asks_about_is_read_and_its_hashes_go_in_walk_order(root: Path) -> None:
    rels = [f"d{n % 7}/f{n:04}.jpg" for n in range(2600)]
    for rel in rels:
        _file(root, rel, b"x")
    walk_order = sorted(rels)
    asked = [r for i, r in enumerate(walk_order) if i % 5 != 0]
    clip = {r: f"ast_{n}" for n, r in enumerate(walk_order)}
    plan = {r: ("hash", "changed", clip[r]) for r in asked}
    plan |= {r: ("unchanged", None, clip[r]) for r in walk_order if r not in plan}
    server = FakeScans(plan=plan, decide=lambda rel, sha: {"decision": "unchanged", "asset_id": clip[rel]})
    read: list[str] = []
    lock = threading.Lock()

    def sha_of(path: Path) -> str:
        with lock:
            read.append(f"{path.parent.name}/{path.name}")
        return _sha(path.name)

    with patch("src.processing.scan.content_hash", side_effect=sha_of):
        stats = _scan(server, root, concurrency=4)

    assert sorted(read) == sorted(asked)  # the unchanged aren't read
    batches = server.hashed()
    assert [len(b) for b in batches] == [1000, 1000, len(asked) - 2000]
    # In walk order: the first file sent with a gone clip's content takes it.
    assert [f["rel_path"] for b in batches for f in b] == asked
    assert stats.unchanged == 2600


def test_hashing_keeps_at_most_twice_its_concurrency_waiting(tmp_path: Path) -> None:
    started: list[str] = []
    release = threading.Event()

    def slow(path: Path) -> str:
        started.append(path.name)
        release.wait(60)
        return _sha(path.name)

    files = [{"rel_path": f"f{n:02}.jpg"} for n in range(20)]
    for f in files:
        (tmp_path / f["rel_path"]).write_bytes(b"x")
    with patch("src.processing.scan.content_hash", side_effect=slow):
        hashes = _hashed(Folder(tmp_path), files, 2)
        threading.Timer(0.2, release.set).start()
        first = next(hashes)
        assert len(started) <= 4  # never every file submitted at once
        rest = list(hashes)
    assert [f["rel_path"] for f, _ in [first, *rest]] == [f["rel_path"] for f in files]
    assert all(hashed == (_sha(f["rel_path"]), None) for f, hashed in [first, *rest])


def test_a_file_that_cant_be_read_is_left_as_it_is(root: Path) -> None:
    # Found, so neither changed nor gone; no hash is sent. Counted as before
    # the server compared: unchanged where a clip is, skipped where a person
    # removed a file, else tried as new.
    for rel in ("known.jpg", "removed.jpg", "moved.jpg"):
        _photo(root, rel)
    server = FakeScans(plan={"known.jpg": ("hash", "changed", "ast_k"), "removed.jpg": ("hash", "removed", None),
                             "moved.jpg": ("hash", "move", None)}, gone=1)
    with patch("src.processing.scan.content_hash", side_effect=OSError("unreadable")):
        stats = _scan(server, root, allow_moves=True)
    assert server.hashed() == []
    assert stats.unchanged == 1
    assert server.ingested() == ["moved.jpg"]
    assert stats.failed_paths == []


# ---------------------------------------------------------------------------
# What the server decides
# ---------------------------------------------------------------------------


def test_what_the_server_says_to_upload_is_uploaded_after_the_finish(root: Path) -> None:
    for n, rel in enumerate(["changed.jpg", "force.jpg", "moved.jpg", "new.jpg", "new-path.jpg", "removed.jpg",
                             "same.jpg", "touched.jpg"]):
        _photo(root, rel, shade=n)
    decided = {
        "changed.jpg": {"decision": "upload", "asset_id": "ast_c"},  # other content: a new clip
        "moved.jpg": {"decision": "moved", "asset_id": "ast_m", "moved_from": "was/moved.jpg"},
        "new-path.jpg": {"decision": "upload"},
        "touched.jpg": {"decision": "unchanged", "asset_id": "ast_t"},
    }
    server = FakeScans(plan={
        "changed.jpg": ("hash", "changed", "ast_c"),
        "force.jpg": ("upload", "force", "ast_f"),
        "moved.jpg": ("hash", "move", None),
        "new.jpg": ("upload", "new", None),
        "new-path.jpg": ("hash", "move", None),
        "removed.jpg": ("skipped", "removed", None),
        "same.jpg": ("unchanged", None, "ast_s"),
        "touched.jpg": ("hash", "changed", "ast_t"),
    }, decide=lambda rel, sha: decided[rel], gone=1)

    with patch("src.processing.scan.content_hash", side_effect=lambda p: _sha(p.name)):
        stats = _scan(server, root, allow_moves=True)

    # New first, then changed, each in walk order; never before the finish
    # (a file whose content was just archived comes back at its new path).
    assert server.ingested() == ["new.jpg", "new-path.jpg", "force.jpg", "changed.jpg"]
    calls = server.calls()
    assert calls.index(f"POST /v1/scans/{SCAN_ID}/finish") < calls.index("POST /v1/ingest")
    assert (stats.new, stats.changed, stats.unchanged, stats.moved, stats.deleted) == (2, 2, 2, 1, 1)
    assert stats.failed_paths == []
    # The hashes are the files' own.
    assert {f["rel_path"]: f["content_hash"] for b in server.hashed() for f in b} == {
        rel: _sha(rel) for rel in ("changed.jpg", "moved.jpg", "new-path.jpg", "touched.jpg")}


def test_another_clip_just_put_at_a_moves_path_sends_the_hashes_again(root: Path) -> None:
    _photo(root, "moved.jpg")
    taken = answer(409, {"error": {"code": "path_taken", "message": "send them again", "details": {}}})
    server = FakeScans(plan={"moved.jpg": ("hash", "move", None)}, hashes=[taken], gone=1,
                       decide=lambda rel, sha: {"decision": "unchanged", "asset_id": "ast_1"})
    stats = _scan(server, root, allow_moves=True)
    assert len(server.hashed()) == 2 and server.hashed()[0] == server.hashed()[1]
    assert stats.unchanged == 1


def test_the_unchanged_files_fill_the_schedulers_preview_cache(root: Path) -> None:
    # On a machine apart from the API (cache_previews): its producers read them there.
    _photo(root, "a.jpg")
    _photo(root, "b.jpg")
    server = FakeScans(default=("unchanged", None, None),
                       plan={"a.jpg": ("unchanged", None, "ast_a"), "b.jpg": ("hash", "changed", "ast_b")},
                       decide=lambda rel, sha: {"decision": "unchanged", "asset_id": "ast_b"})
    _scan(server, root, cache_previews=True)
    assert [r.path for r in server.requests if r.path.endswith("/artifacts/proxy")] == [
        "/v1/assets/ast_a/artifacts/proxy", "/v1/assets/ast_b/artifacts/proxy"]


def test_where_previews_are_read_in_place_a_scan_keeps_no_copies(root: Path, tmp_path: Path) -> None:
    # The scheduler beside the API, as every client (cache_previews=False,
    # the default): nothing cached, nothing downloaded, and the unchanged
    # only counted, never listed.
    _photo(root, "a.jpg")
    _photo(root, "b.jpg", shade=9)
    server = FakeScans(default=("upload", "new", None), plan={"a.jpg": ("unchanged", None, "ast_a")})
    stats = _scan(server, root, cache_previews=False)
    assert server.ingested() == ["b.jpg"] and stats.unchanged == 1
    assert {r.params.get("action") for r in server.requests if r.path.endswith("/plan")} == {"upload,hash,skipped"}
    assert not [r for r in server.requests if r.path.endswith("/artifacts/proxy")]
    assert not (tmp_path / "cache" / "lumiverb" / "proxies").exists()


# ---------------------------------------------------------------------------
# The finish
# ---------------------------------------------------------------------------


def test_clips_handed_to_an_empty_copy_of_their_file_count_as_moved(root: Path) -> None:
    _photo(root, "f0.jpg")
    server = FakeScans(default=("unchanged", None, "ast_0"), gone=2,
                       finish=[answer(200, finished(archived=1, moved=1, handed_over=1))])
    stats = _scan(server, root, allow_moves=True)
    assert (stats.deleted, stats.moved) == (1, 1)


def test_the_server_asking_about_mass_missing_archives_nothing_but_uploads_the_rest(root: Path) -> None:
    _photo(root, "new.jpg")
    server = FakeScans(gone=90, finish=[mass_missing(90)])
    stats = _scan(server, root, allow_moves=True)
    assert server.finishes() == [{"allow_mass_missing": False}]
    assert (stats.mass_missing, stats.deleted) == (90, 0)
    assert server.abandoned()
    assert server.ingested() == ["new.jpg"]  # as before: what's new is still new


def test_allow_mass_delete_says_so_in_the_finish(root: Path) -> None:
    _photo(root, "new.jpg")
    server = FakeScans(gone=90)
    stats = _scan(server, root, allow_moves=True, allow_mass_delete=True)
    assert server.finishes() == [{"allow_mass_missing": True}]
    assert stats.deleted == 90 and not server.abandoned()


# ---------------------------------------------------------------------------
# Moves the server asks about (the CLI), and dry runs
# ---------------------------------------------------------------------------


def _asked(root: Path, choice: str, **server_args) -> tuple[FakeScans, ScanStats]:
    _photo(root, "keep.jpg")
    _photo(root, "sorted/a.jpg")
    server = FakeScans(plan={"sorted/a.jpg": ("hash", "move", None)}, gone=1, **server_args,
                       decide=lambda rel, sha: {"decision": "move_candidate", "asset_id": "ast_a",
                                                "moved_from": "inbox/a.jpg"})
    with patch("builtins.input", side_effect=[choice] if choice != "eof" else EOFError):
        stats = _scan(server, root)
    return server, stats


def test_moves_asked_about_and_applied(root: Path) -> None:
    server, stats = _asked(root, "1", moves=[answer(200, {"moved": 1, "skipped": 0, "upload": []})])
    assert server.started()["moves"] == "ask"
    assert [r.body for r in server.sent("POST", f"/v1/scans/{SCAN_ID}/moves")] == [{"apply": True}]
    calls = server.calls()
    assert calls.index(f"POST /v1/scans/{SCAN_ID}/moves") < calls.index(f"POST /v1/scans/{SCAN_ID}/finish")
    assert stats.moved == 1 and "sorted/a.jpg" not in server.ingested()


def test_a_move_that_couldnt_be_made_is_uploaded(root: Path) -> None:
    server, stats = _asked(root, "1", moves=[answer(200, {"moved": 0, "skipped": 0, "upload": ["sorted/a.jpg"]})])
    assert "sorted/a.jpg" in server.ingested() and stats.moved == 0


def test_moves_asked_about_and_skipped_take_no_part(root: Path) -> None:
    server, stats = _asked(root, "2", moves=[answer(200, {"moved": 0, "skipped": 1, "upload": []})])
    assert [r.body for r in server.sent("POST", f"/v1/scans/{SCAN_ID}/moves")] == [{"apply": False}]
    assert "sorted/a.jpg" not in server.ingested()


@pytest.mark.parametrize("choice", ["3", "eof"])
def test_an_aborted_scan_changes_nothing(root: Path, choice: str) -> None:
    server, stats = _asked(root, choice)
    assert server.abandoned()
    assert not server.sent("POST", f"/v1/scans/{SCAN_ID}/moves") and not server.finishes()
    assert server.ingested() == []


@pytest.mark.parametrize("allow_moves", [False, True])
def test_a_dry_run_decides_everything_and_drops_the_scan(root: Path, allow_moves: bool) -> None:
    _photo(root, "new.jpg")
    _photo(root, "sorted/a.jpg")
    server = FakeScans(plan={"sorted/a.jpg": ("hash", "move", None)}, gone=2,
                       decide=lambda rel, sha: {"decision": "moved" if allow_moves else "move_candidate",
                                                "asset_id": "ast_a", "moved_from": "inbox/a.jpg"},
                       finish=[answer(200, finished(archived=1, moved=1, dry_run=True))])
    with patch("builtins.input", side_effect=AssertionError("a dry run never asks")):
        _scan(server, root, dry_run=True, allow_moves=allow_moves)
    assert server.started()["dry_run"] is True
    assert len(server.hashed()) == 1  # decided, to say what would happen
    assert not server.sent("POST", f"/v1/scans/{SCAN_ID}/moves")
    assert server.finishes() == [{"allow_mass_missing": False}] and server.abandoned()
    assert server.ingested() == []


# ---------------------------------------------------------------------------
# The proxy cache
# ---------------------------------------------------------------------------


class TestPopulateCacheForUnchanged:
    """Test proxy download for unchanged files with missing cache."""

    def test_skips_cached(self, tmp_path):
        """Files already in cache are skipped."""
        from src.processing.proxy.proxy_cache import ProxyCache
        cache = ProxyCache()
        cache._dir = tmp_path
        cache.put_scan("id-1", b"proxy", "sha")

        stats = ScanStats()
        console = Console(quiet=True)
        mock_client = MagicMock()

        _populate_cache_for_unchanged(
            mock_client,
            [{"asset_id": "id-1", "content_hash": "sha256:1"}],
            cache, stats, console,
        )
        mock_client._client.get.assert_not_called()
        assert stats.cache_populated == 0

    def test_downloads_missing(self, tmp_path):
        """Files not in cache are downloaded from server."""
        from src.processing.proxy.proxy_cache import ProxyCache
        cache = ProxyCache()
        cache._dir = tmp_path

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"server-proxy-bytes"

        mock_client = MagicMock()
        mock_client._client.get.return_value = mock_resp

        stats = ScanStats()
        console = Console(quiet=True)

        _populate_cache_for_unchanged(
            mock_client,
            [{"asset_id": "id-1", "content_hash": "sha256:1"}],
            cache, stats, console,
        )
        assert stats.cache_populated == 1
        assert (tmp_path / "id-1").read_bytes() == b"server-proxy-bytes"
        assert (tmp_path / "id-1.sha").read_text() == "sha256:1"
