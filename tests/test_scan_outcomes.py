# ruff: noqa: F811 — pytest fixtures are named as parameters
"""What a scan leaves on the server.

ADR-018 decision 12: today the scan works out on the client what's new,
changed, unchanged, moved, missing and skipped (src/processing/scan.py);
phase 2 moves those decisions to the server. These tests pin what a scan
leaves behind, end to end: the real run_scan, as the scheduler calls it,
over a real folder of real files, against the real API and its database.
They look only at the server's state afterwards (each clip's path, content,
size, time and whether it's in sight or archived as missing, which clip a
path ends up with, what people did to a clip) and at which files the scan
says failed (the scheduler retries those), never at how the scan got there,
so the same tests must pass unchanged when the comparison moves.

Each test is one scenario: a folder and the server's clips, a change on
disk, a scan with given options, and what the server holds after. Most end
by scanning again to show the outcome is settled: nothing changes.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from rich.console import Console
from sqlalchemy import text

from src.processing.scan import run_scan
from tests.machine_lineage import ingest_made
from tests.producer_fakes import scheduler_client
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_archive_by_hand import _db

pytestmark = pytest.mark.slow

HOUR = 3600.0


# ---------------------------------------------------------------------------
# The harness: a library per test, its folder, the scheduler's client
# ---------------------------------------------------------------------------


@dataclass
class Lib:
    root: Path
    library: dict

    @property
    def id(self) -> str:
        return self.library["library_id"]


@pytest.fixture(scope="module")
def api(env):
    """The scheduler's API client (its own key), talking to the app in-process."""
    return scheduler_client(env[4])


@pytest.fixture(autouse=True)
def _cache_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scan's proxy cache in the test's own folder, never ~/.cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
def lib(env, tmp_path: Path):
    """A library of its own, its folder empty, in an account that follows moves."""
    client, headers, *_ = env
    root = tmp_path / "library"
    root.mkdir()
    r = client.post("/v1/libraries", json={"name": f"Scan {uuid.uuid4().hex[:10]}", "root_path": str(root)},
                    headers=headers)
    assert r.status_code == 200, r.text
    _follow_moves(env, True)
    yield Lib(root, r.json())
    _follow_moves(env, True)


def _follow_moves(env, on: bool) -> None:
    client, headers, *_ = env
    r = client.patch("/v1/tenant/settings", json={"follow_moves": on}, headers=headers)
    assert r.status_code == 200 and r.json()["follow_moves"] is on, r.text


def _scan(api, lib: Lib, **options):
    """A scan as the scheduler runs one (moves applied, the mass-missing
    guard left to a person), one file at a time so that which of two
    ingests commits first never decides the outcome.

    No file fails in any of these scenarios: the scheduler tries a failed
    file again and again (scans.py _note_failures), so what it's told
    failed is an outcome too. A file a person removed is skipped, not
    tried and refused."""
    kwargs = {"allow_moves": True, "allow_mass_delete": False, "concurrency": 1, "console": Console(quiet=True)}
    kwargs.update(options)
    stats = run_scan(api, lib.library, **kwargs)
    assert stats.failed_paths == [], stats.failed_paths
    return stats


def _settled(env, api, lib: Lib) -> None:
    """Scanning again with nothing changed on disk changes nothing: what a
    scan leaves is what the next one expects."""
    before = _rows(env, lib)
    _scan(api, lib)
    assert _rows(env, lib) == before


# ---------------------------------------------------------------------------
# Files on disk
# ---------------------------------------------------------------------------


def _ago(seconds: float) -> float:
    return time.time() - seconds


def _set_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def _image(lib: Lib, rel: str, n: int, *, mtime: float | None = None) -> Path:
    """A small PNG whose content is n's own. Stored uncompressed, so every
    one is the same size: only their content tells them apart. Written an
    hour ago unless said otherwise (a file written just now is still settling)."""
    path = lib.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), (n % 256, (n // 256) % 256, 128)).save(path, format="PNG", compress_level=0)
    _set_mtime(path, _ago(HOUR) if mtime is None else mtime)
    return path


def _video(lib: Lib, rel: str, *, mtime: float | None = None) -> Path:
    """A one-second MP4."""
    path = lib.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    _set_mtime(path, _ago(HOUR) if mtime is None else mtime)
    return path


def _move(lib: Lib, old: str, new: str) -> None:
    """A move or rename on disk: content and modified time go with the file."""
    (lib.root / new).parent.mkdir(parents=True, exist_ok=True)
    os.rename(lib.root / old, lib.root / new)


def _copy(lib: Lib, old: str, new: str) -> None:
    """A copy, written an hour ago (so settled), with the same content."""
    (lib.root / new).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(lib.root / old, lib.root / new)
    _set_mtime(lib.root / new, _ago(HOUR))


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


# ---------------------------------------------------------------------------
# The server's state
# ---------------------------------------------------------------------------


def _rows(env, lib: Lib) -> list[dict]:
    """Every clip of the library, in sight or not, oldest first."""
    with _db(env) as session:
        rows = session.execute(text(
            "SELECT asset_id, rel_path, content_hash, file_size, file_mtime, media_type, deleted_at, deleted_reason,"
            "       video_preview_key, note, updated_at"
            "  FROM assets WHERE library_id = :lib ORDER BY created_at, asset_id"), {"lib": lib.id}).mappings().all()
    return [dict(r) for r in rows]


def _in_sight(env, lib: Lib) -> dict[str, dict]:
    """The clips in sight, by path."""
    return {r["rel_path"]: r for r in _rows(env, lib) if r["deleted_at"] is None}


def _clip(env, lib: Lib, asset_id: str) -> dict | None:
    return next((r for r in _rows(env, lib) if r["asset_id"] == asset_id), None)


def _missing(env, lib: Lib) -> dict[str, dict]:
    """The clips archived because their files weren't found, by path."""
    return {r["rel_path"]: r for r in _rows(env, lib) if r["deleted_reason"] == "missing"}


def _note(env, asset_id: str, note: str) -> None:
    client, headers, *_ = env
    r = client.put(f"/v1/assets/{asset_id}/note", json={"text": note}, headers=headers)
    assert r.status_code in (200, 204), r.text


def _project(env, *asset_ids: str) -> str:
    client, headers, *_ = env
    r = client.post("/v1/projects", json={"name": f"P {uuid.uuid4().hex[:10]}", "asset_ids": list(asset_ids)},
                    headers=headers)
    assert r.status_code in (200, 201), r.text
    return r.json()["project_id"]


def _project_clips(env, project_id: str) -> set[str]:
    with _db(env) as session:
        return set(session.execute(text("SELECT asset_id FROM project_assets WHERE project_id = :p"),
                                   {"p": project_id}).scalars())


def _seed(env, lib: Lib, rel: str, *, media_type: str = "image") -> str:
    """A clip on the server for the file at rel as a scan records it (its
    content, size and modified time), made without a scan: no preview of a
    video, say, as when its upload never arrived."""
    client, headers, *_ = env
    path = lib.root / rel
    sha = _sha(path)
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (1, 2, 3)).save(buf, format="JPEG")
    buf.seek(0)
    data = {"library_id": lib.id, "rel_path": rel, "file_size": str(path.stat().st_size),
            "file_mtime": _mtime(path).isoformat(), "media_type": media_type, "width": "16", "height": "16",
            "exif": json.dumps({"content_hash": sha}), "lineage": ingest_made(sha)}
    r = client.post("/v1/ingest", data=data, files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


# ---------------------------------------------------------------------------
# New, unchanged, changed
# ---------------------------------------------------------------------------


def test_new_files_are_ingested_and_a_second_scan_changes_nothing(env, api, lib):
    a = _image(lib, "a.png", 1)
    b = _image(lib, "day 1/b.png", 2)
    v = _video(lib, "day 1/clip.mp4")

    _scan(api, lib)

    clips = _in_sight(env, lib)
    assert set(clips) == {"a.png", "day 1/b.png", "day 1/clip.mp4"}
    for rel, path in (("a.png", a), ("day 1/b.png", b), ("day 1/clip.mp4", v)):
        assert clips[rel]["content_hash"] == _sha(path)
        assert clips[rel]["file_size"] == path.stat().st_size
        assert clips[rel]["file_mtime"] == _mtime(path)
    assert clips["a.png"]["media_type"] == "image"
    assert clips["day 1/clip.mp4"]["media_type"] == "video"
    assert clips["day 1/clip.mp4"]["video_preview_key"] is not None
    assert _missing(env, lib) == {}
    _settled(env, api, lib)


@pytest.mark.parametrize("follow_moves", [True, False])
def test_a_changed_file_at_the_same_path_is_a_new_clip_and_the_old_one_goes_missing(env, api, lib, follow_moves):
    # Robert, Oct 9: "a binary different file with the same name is a
    # completely new file", and the old one goes missing, whether or not the
    # account follows moves.
    _follow_moves(env, follow_moves)
    _image(lib, "a.png", 1)
    _scan(api, lib)
    old = _in_sight(env, lib)["a.png"]
    _note(env, old["asset_id"], "the first take")
    project = _project(env, old["asset_id"])

    changed = _image(lib, "a.png", 2, mtime=_ago(HOUR / 2))
    _scan(api, lib)

    new = _in_sight(env, lib)["a.png"]
    assert new["asset_id"] != old["asset_id"]
    assert new["content_hash"] == _sha(changed) and new["note"] is None  # nothing carries over
    was = _clip(env, lib, old["asset_id"])
    assert was["deleted_reason"] == "missing" and was["rel_path"] == "a.png"  # archived where it was
    assert was["content_hash"] == old["content_hash"] and was["note"] == "the first take"
    assert _project_clips(env, project) == {old["asset_id"]}  # still in its project, as missing
    _settled(env, api, lib)


def test_the_old_content_back_at_its_path_brings_its_clip_back(env, api, lib):
    first = _image(lib, "a.png", 1, mtime=_ago(3 * HOUR))
    first_sha = _sha(first)
    _scan(api, lib)
    old = _in_sight(env, lib)["a.png"]
    _image(lib, "a.png", 2, mtime=_ago(2 * HOUR))
    _scan(api, lib)
    second = _in_sight(env, lib)["a.png"]

    _image(lib, "a.png", 1, mtime=_ago(HOUR))
    _scan(api, lib)

    back = _in_sight(env, lib)["a.png"]
    assert back["asset_id"] == old["asset_id"] and back["content_hash"] == first_sha
    assert _missing(env, lib)["a.png"]["asset_id"] == second["asset_id"]
    assert len(_rows(env, lib)) == 2
    _settled(env, api, lib)


def test_a_touched_file_stays_its_clip_and_its_new_time_is_recorded(env, api, lib):
    a = _image(lib, "a.png", 1, mtime=_ago(2 * HOUR))
    _scan(api, lib)
    before = _in_sight(env, lib)["a.png"]

    _set_mtime(a, _ago(HOUR))  # same content, a new modified time
    _scan(api, lib)

    after = _in_sight(env, lib)["a.png"]
    assert after["asset_id"] == before["asset_id"] and after["content_hash"] == before["content_hash"]
    # The server has the new time, so the next scan finds the file unchanged
    # by its size and time alone.
    assert after["file_mtime"] == _mtime(a) != before["file_mtime"]
    assert len(_rows(env, lib)) == 1
    _settled(env, api, lib)


def test_force_reads_every_file_again_and_keeps_every_clip(env, api, lib):
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    before = _in_sight(env, lib)
    _note(env, before["a.png"]["asset_id"], "keeper")

    _scan(api, lib, force=True)

    after = _in_sight(env, lib)
    assert {rel: c["asset_id"] for rel, c in after.items()} == {rel: c["asset_id"] for rel, c in before.items()}
    assert after["a.png"]["note"] == "keeper"
    assert len(_rows(env, lib)) == 2
    _settled(env, api, lib)


def test_thorough_catches_new_content_with_the_same_size_and_time(env, api, lib):
    a = _image(lib, "a.png", 1)
    _scan(api, lib)
    old = _in_sight(env, lib)["a.png"]
    _image(lib, "a.png", 2, mtime=a.stat().st_mtime)  # same size, same time, other content
    assert a.stat().st_size == old["file_size"] and _mtime(a) == old["file_mtime"]

    _scan(api, lib)  # by size and time it's unchanged
    assert _in_sight(env, lib)["a.png"]["asset_id"] == old["asset_id"]
    assert _in_sight(env, lib)["a.png"]["content_hash"] == old["content_hash"]

    _scan(api, lib, thorough=True)
    new = _in_sight(env, lib)["a.png"]
    assert new["asset_id"] != old["asset_id"] and new["content_hash"] == _sha(a)
    assert _missing(env, lib)["a.png"]["asset_id"] == old["asset_id"]
    _settled(env, api, lib)


def test_a_video_whose_preview_never_arrived_gets_one(env, api, lib):
    v = _video(lib, "clip.mp4")
    clip = _seed(env, lib, "clip.mp4", media_type="video")  # the clip made, its preview's upload lost
    assert _in_sight(env, lib)["clip.mp4"]["video_preview_key"] is None

    _scan(api, lib)

    now = _in_sight(env, lib)["clip.mp4"]
    assert now["asset_id"] == clip and now["content_hash"] == _sha(v)
    assert now["video_preview_key"] is not None
    assert len(_rows(env, lib)) == 1
    _settled(env, api, lib)


# ---------------------------------------------------------------------------
# Moves, renames and copies
# ---------------------------------------------------------------------------


def test_a_moved_file_keeps_its_clip_and_what_people_did_to_it(env, api, lib):
    _image(lib, "inbox/a.png", 1)
    _image(lib, "inbox/b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["inbox/a.png"]
    _note(env, a["asset_id"], "keeper")
    project = _project(env, a["asset_id"])

    _move(lib, "inbox/a.png", "sorted/a renamed.png")
    _scan(api, lib)

    clips = _in_sight(env, lib)
    assert set(clips) == {"sorted/a renamed.png", "inbox/b.png"}
    moved = clips["sorted/a renamed.png"]
    assert moved["asset_id"] == a["asset_id"] and moved["content_hash"] == a["content_hash"] and moved["note"] == "keeper"
    assert _project_clips(env, project) == {a["asset_id"]}
    assert len(_rows(env, lib)) == 2  # nothing archived, nothing new
    _settled(env, api, lib)


def test_a_renamed_folder_moves_every_clip_in_it(env, api, lib):
    for n in range(3):
        _image(lib, f"2026-10-01/{n}.png", n)
    _image(lib, "elsewhere.png", 9)
    _scan(api, lib)
    before = _in_sight(env, lib)

    os.rename(lib.root / "2026-10-01", lib.root / "2026-10-01 Lisbon")
    _scan(api, lib)

    after = _in_sight(env, lib)
    assert set(after) == {f"2026-10-01 Lisbon/{n}.png" for n in range(3)} | {"elsewhere.png"}
    for n in range(3):
        assert after[f"2026-10-01 Lisbon/{n}.png"]["asset_id"] == before[f"2026-10-01/{n}.png"]["asset_id"]
    assert len(_rows(env, lib)) == 4
    _settled(env, api, lib)


def test_with_moves_not_followed_a_moved_file_is_a_new_clip_and_the_old_one_goes_missing(env, api, lib):
    _image(lib, "inbox/a.png", 1)
    _scan(api, lib)
    old = _in_sight(env, lib)["inbox/a.png"]
    _note(env, old["asset_id"], "keeper")
    _follow_moves(env, False)

    _move(lib, "inbox/a.png", "sorted/a.png")
    _scan(api, lib)

    new = _in_sight(env, lib)
    assert set(new) == {"sorted/a.png"}
    assert new["sorted/a.png"]["asset_id"] != old["asset_id"] and new["sorted/a.png"]["note"] is None
    gone = _missing(env, lib)
    assert set(gone) == {"inbox/a.png"}
    assert gone["inbox/a.png"]["asset_id"] == old["asset_id"] and gone["inbox/a.png"]["note"] == "keeper"
    _settled(env, api, lib)


def test_a_copy_is_a_clip_of_its_own(env, api, lib):
    _image(lib, "a.png", 1)
    _scan(api, lib)
    original = _in_sight(env, lib)["a.png"]
    _note(env, original["asset_id"], "the original")

    _copy(lib, "a.png", "copies/a.png")
    _scan(api, lib)

    clips = _in_sight(env, lib)
    assert set(clips) == {"a.png", "copies/a.png"}
    assert clips["a.png"]["asset_id"] == original["asset_id"] and clips["a.png"]["note"] == "the original"
    assert clips["copies/a.png"]["asset_id"] != original["asset_id"]
    assert clips["copies/a.png"]["content_hash"] == original["content_hash"] and clips["copies/a.png"]["note"] is None
    assert _missing(env, lib) == {}
    _settled(env, api, lib)


def test_a_copy_then_the_original_deleted_moves_the_clip_to_the_copy(env, api, lib):
    # Copy, then delete: a move between drives, or a careful person's.
    _image(lib, "a.png", 1)
    _scan(api, lib)
    original = _in_sight(env, lib)["a.png"]
    _note(env, original["asset_id"], "the original")
    _copy(lib, "a.png", "copies/a.png")
    _scan(api, lib)
    copy = _in_sight(env, lib)["copies/a.png"]

    (lib.root / "a.png").unlink()
    _scan(api, lib)

    clips = _in_sight(env, lib)
    assert set(clips) == {"copies/a.png"}
    assert clips["copies/a.png"]["asset_id"] == original["asset_id"]
    assert clips["copies/a.png"]["note"] == "the original"
    assert _clip(env, lib, copy["asset_id"]) is None  # the copy's own clip is gone for good
    assert _missing(env, lib) == {}
    _settled(env, api, lib)


def test_a_copy_someone_worked_on_keeps_its_clip_when_the_original_goes(env, api, lib):
    _image(lib, "a.png", 1)
    _scan(api, lib)
    original = _in_sight(env, lib)["a.png"]
    _copy(lib, "a.png", "copies/a.png")
    _scan(api, lib)
    copy = _in_sight(env, lib)["copies/a.png"]
    _note(env, copy["asset_id"], "the copy's own")

    (lib.root / "a.png").unlink()
    _scan(api, lib)

    assert _in_sight(env, lib)["copies/a.png"]["asset_id"] == copy["asset_id"]
    assert _missing(env, lib)["a.png"]["asset_id"] == original["asset_id"]
    _settled(env, api, lib)


def test_a_copy_made_then_the_original_overwritten_takes_the_clip_with_it(env, api, lib):
    _image(lib, "a.png", 1)
    _scan(api, lib)
    original = _in_sight(env, lib)["a.png"]
    _note(env, original["asset_id"], "the original")

    _copy(lib, "a.png", "copies/a.png")
    overwritten = _image(lib, "a.png", 2, mtime=_ago(HOUR / 2))
    _scan(api, lib)

    clips = _in_sight(env, lib)
    assert set(clips) == {"a.png", "copies/a.png"}
    assert clips["copies/a.png"]["asset_id"] == original["asset_id"]
    assert clips["copies/a.png"]["note"] == "the original"
    assert clips["a.png"]["asset_id"] != original["asset_id"] and clips["a.png"]["content_hash"] == _sha(overwritten)
    assert _missing(env, lib) == {}
    assert len(_rows(env, lib)) == 2  # the copy's own clip, made in the same scan, gone for good
    _settled(env, api, lib)


def test_with_force_a_moved_file_still_keeps_its_clip(env, api, lib):
    # force looks for no moves: the old path is archived, then the content at
    # the new path brings its clip back there.
    _image(lib, "inbox/a.png", 1)
    _scan(api, lib)
    a = _in_sight(env, lib)["inbox/a.png"]
    _note(env, a["asset_id"], "keeper")

    _move(lib, "inbox/a.png", "sorted/a.png")
    _scan(api, lib, force=True)

    clips = _in_sight(env, lib)
    assert set(clips) == {"sorted/a.png"}
    assert clips["sorted/a.png"]["asset_id"] == a["asset_id"] and clips["sorted/a.png"]["note"] == "keeper"
    assert len(_rows(env, lib)) == 1
    _settled(env, api, lib)


def test_a_moved_and_edited_file_is_a_new_clip(env, api, lib):
    # A move is the same content at a new path: new content anywhere is new.
    _image(lib, "inbox/a.png", 1)
    _scan(api, lib)
    a = _in_sight(env, lib)["inbox/a.png"]

    (lib.root / "inbox/a.png").unlink()
    edited = _image(lib, "sorted/a.png", 2)
    _scan(api, lib)

    clips = _in_sight(env, lib)
    assert set(clips) == {"sorted/a.png"}
    assert clips["sorted/a.png"]["asset_id"] != a["asset_id"] and clips["sorted/a.png"]["content_hash"] == _sha(edited)
    assert _missing(env, lib)["inbox/a.png"]["asset_id"] == a["asset_id"]
    _settled(env, api, lib)


def test_a_move_seen_first_by_a_scan_of_its_new_folder_is_a_move_by_the_next_full_scan(env, api, lib):
    # The scheduler scans the folder that covers what changed: a file moved
    # out of a folder that scan doesn't cover looks like a copy until the
    # library's full scan finds its old path empty.
    _image(lib, "inbox/a.png", 1)
    _image(lib, "inbox/b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["inbox/a.png"]
    _note(env, a["asset_id"], "keeper")

    _move(lib, "inbox/a.png", "sorted/a.png")
    _scan(api, lib, path_prefix="sorted")
    clips = _in_sight(env, lib)
    assert set(clips) == {"inbox/a.png", "inbox/b.png", "sorted/a.png"}
    assert clips["inbox/a.png"]["asset_id"] == a["asset_id"]

    _scan(api, lib)
    clips = _in_sight(env, lib)
    assert set(clips) == {"inbox/b.png", "sorted/a.png"}
    assert clips["sorted/a.png"]["asset_id"] == a["asset_id"] and clips["sorted/a.png"]["note"] == "keeper"
    assert _missing(env, lib) == {} and len(_rows(env, lib)) == 2  # the copy's clip gone for good
    _settled(env, api, lib)


def test_skip_moves_leaves_the_old_clip_in_sight_and_makes_a_new_one(env, api, lib):
    # Pinned as is: --skip-moves is documented as "don't update paths, don't
    # treat as new/deleted" (the CLI's help, docs/cursor-cli.md), but it
    # skips looking for moves altogether, so the file at its new path is
    # ingested as a new clip, and the old clip stays in sight at a path with
    # no file. The next scan without it puts things right.
    _image(lib, "inbox/a.png", 1)
    _image(lib, "inbox/b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["inbox/a.png"]
    _note(env, a["asset_id"], "keeper")

    _move(lib, "inbox/a.png", "sorted/a.png")
    (lib.root / "inbox/b.png").unlink()  # really gone: held back as well
    _scan(api, lib, allow_moves=False, skip_moves=True)

    clips = _in_sight(env, lib)
    assert set(clips) == {"inbox/a.png", "inbox/b.png", "sorted/a.png"}
    assert clips["inbox/a.png"]["asset_id"] == a["asset_id"]
    assert clips["sorted/a.png"]["asset_id"] != a["asset_id"]
    assert _missing(env, lib) == {}

    _scan(api, lib)
    clips = _in_sight(env, lib)
    assert set(clips) == {"sorted/a.png"}
    assert clips["sorted/a.png"]["asset_id"] == a["asset_id"] and clips["sorted/a.png"]["note"] == "keeper"
    assert set(_missing(env, lib)) == {"inbox/b.png"}
    _settled(env, api, lib)


# ---------------------------------------------------------------------------
# Missing files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("follow_moves", [True, False])
def test_a_deleted_file_goes_missing_and_comes_back_as_its_clip(env, api, lib, follow_moves):
    _follow_moves(env, follow_moves)
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["a.png"]
    _note(env, a["asset_id"], "keeper")

    (lib.root / "a.png").unlink()
    _scan(api, lib)
    assert set(_in_sight(env, lib)) == {"b.png"}
    gone = _missing(env, lib)["a.png"]
    assert gone["asset_id"] == a["asset_id"] and gone["note"] == "keeper"

    returned = _image(lib, "a.png", 1, mtime=_ago(HOUR / 2))  # the same content, back where it was
    _scan(api, lib)
    back = _in_sight(env, lib)["a.png"]
    assert back["asset_id"] == a["asset_id"] and back["note"] == "keeper"
    assert back["file_mtime"] == _mtime(returned)
    assert _missing(env, lib) == {} and len(_rows(env, lib)) == 2
    _settled(env, api, lib)


def test_a_missing_files_content_found_at_another_path_brings_its_clip_there(env, api, lib):
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["a.png"]
    _note(env, a["asset_id"], "keeper")
    (lib.root / "a.png").unlink()
    _scan(api, lib)
    assert _missing(env, lib)["a.png"]["asset_id"] == a["asset_id"]

    _image(lib, "found/a.png", 1)
    _scan(api, lib)

    clips = _in_sight(env, lib)
    assert set(clips) == {"found/a.png", "b.png"}
    assert clips["found/a.png"]["asset_id"] == a["asset_id"] and clips["found/a.png"]["note"] == "keeper"
    assert _missing(env, lib) == {} and len(_rows(env, lib)) == 2
    _settled(env, api, lib)


def test_with_moves_not_followed_a_missing_files_content_elsewhere_is_a_new_clip(env, api, lib):
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["a.png"]
    _follow_moves(env, False)
    (lib.root / "a.png").unlink()
    _scan(api, lib)

    _image(lib, "found/a.png", 1)
    _scan(api, lib)

    assert _in_sight(env, lib)["found/a.png"]["asset_id"] != a["asset_id"]
    assert _missing(env, lib)["a.png"]["asset_id"] == a["asset_id"]
    _settled(env, api, lib)


def test_a_file_moved_while_its_new_copy_settles_goes_missing_then_comes_back_there(env, api, lib):
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["a.png"]
    _note(env, a["asset_id"], "keeper")

    _copy(lib, "a.png", "elsewhere/a.png")
    _set_mtime(lib.root / "elsewhere/a.png", time.time())  # still being copied
    (lib.root / "a.png").unlink()
    _scan(api, lib)
    assert set(_in_sight(env, lib)) == {"b.png"}
    assert _missing(env, lib)["a.png"]["asset_id"] == a["asset_id"]

    _set_mtime(lib.root / "elsewhere/a.png", _ago(HOUR))  # settled
    _scan(api, lib)
    clips = _in_sight(env, lib)
    assert clips["elsewhere/a.png"]["asset_id"] == a["asset_id"] and clips["elsewhere/a.png"]["note"] == "keeper"
    assert _missing(env, lib) == {}
    _settled(env, api, lib)


def test_an_exclude_filter_added_later_archives_its_clips_as_missing(env, api, lib):
    client, headers, *_ = env
    _image(lib, "a.png", 1)
    _image(lib, "drafts/b.png", 2)
    _scan(api, lib)
    b = _in_sight(env, lib)["drafts/b.png"]

    r = client.post(f"/v1/libraries/{lib.id}/filters", json={"type": "exclude", "pattern": "drafts/**"},
                    headers=headers)
    assert r.status_code == 201, r.text
    _scan(api, lib)

    assert set(_in_sight(env, lib)) == {"a.png"}
    assert _missing(env, lib)["drafts/b.png"]["asset_id"] == b["asset_id"]
    _settled(env, api, lib)


# ---------------------------------------------------------------------------
# What a person removed
# ---------------------------------------------------------------------------


def _remove(env, asset_id: str, how: str) -> None:
    client, headers, *_ = env
    if how == "archived":
        r = client.post("/v1/assets/archive", json={"asset_ids": [asset_id]}, headers=headers)
        assert r.status_code == 200, r.text
        return
    r = client.request("DELETE", "/v1/assets", json={"asset_ids": [asset_id], "reason": "user"}, headers=headers)
    assert r.status_code == 200, r.text
    if how == "deleted for good":
        r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [asset_id], "remove_from_projects": True},
                           headers=headers)
        assert r.status_code == 200 and r.json()["deleted"] == 1, r.text


@pytest.mark.parametrize("how", ["trashed", "archived", "deleted for good"])
def test_a_file_a_person_removed_is_skipped_and_another_file_there_is_a_new_clip(env, api, lib, how):
    a_file = _image(lib, "a.png", 1, mtime=_ago(2 * HOUR))
    _image(lib, "b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["a.png"]
    _remove(env, a["asset_id"], how)
    removed = _clip(env, lib, a["asset_id"])

    _scan(api, lib)  # still on disk: Lumiverb never deletes originals
    assert set(_in_sight(env, lib)) == {"b.png"}
    assert _clip(env, lib, a["asset_id"]) == removed

    _set_mtime(a_file, _ago(HOUR))  # touched: read again, and still the file removed
    _scan(api, lib)
    assert set(_in_sight(env, lib)) == {"b.png"}
    assert _clip(env, lib, a["asset_id"]) == removed

    other = _image(lib, "a.png", 3, mtime=_ago(HOUR / 2))
    _scan(api, lib)
    clips = _in_sight(env, lib)
    assert set(clips) == {"a.png", "b.png"}
    assert clips["a.png"]["asset_id"] != a["asset_id"] and clips["a.png"]["content_hash"] == _sha(other)
    assert _clip(env, lib, a["asset_id"]) == removed
    _settled(env, api, lib)


@pytest.mark.parametrize("how", ["trashed", "archived", "deleted for good"])
def test_force_add_brings_back_a_file_a_person_removed(env, api, lib, how):
    """A person's last resort (lumiverb scan --force-add): the file comes
    back, its clip with what people did to it when there's one to come back,
    a new clip when it was deleted for good; later scans keep it."""
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["a.png"]
    _note(env, a["asset_id"], "keep this")
    _remove(env, a["asset_id"], how)

    stats = _scan(api, lib, force_add=True)

    clips = _in_sight(env, lib)
    assert set(clips) == {"a.png", "b.png"}
    if how == "deleted for good":
        assert clips["a.png"]["asset_id"] != a["asset_id"] and clips["a.png"]["note"] is None
        assert stats.restored == 0 and stats.new == 1
    else:
        assert clips["a.png"]["asset_id"] == a["asset_id"] and clips["a.png"]["note"] == "keep this"
        assert stats.restored == 1 and stats.new == 0
    _settled(env, api, lib)


def test_force_add_with_a_path_prefix_brings_back_only_that_folder(env, api, lib):
    _image(lib, "keep/a.png", 1)
    _image(lib, "leave/b.png", 2)
    _scan(api, lib)
    for clip in _in_sight(env, lib).values():
        _remove(env, clip["asset_id"], "trashed")

    _scan(api, lib, force_add=True, path_prefix="keep")

    assert set(_in_sight(env, lib)) == {"keep/a.png"}
    _settled(env, api, lib)


def test_a_trashed_file_moved_to_another_folder_comes_back_as_a_new_clip(env, api, lib):
    # What a scan skips is by path (and the content there): the same content
    # at another path isn't a file a person removed.
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    a = _in_sight(env, lib)["a.png"]
    _remove(env, a["asset_id"], "trashed")

    _move(lib, "a.png", "sorted/a.png")
    _scan(api, lib)

    clips = _in_sight(env, lib)
    assert set(clips) == {"b.png", "sorted/a.png"}
    assert clips["sorted/a.png"]["asset_id"] != a["asset_id"]
    assert _clip(env, lib, a["asset_id"])["deleted_reason"] == "user"
    _settled(env, api, lib)


# ---------------------------------------------------------------------------
# Never taken for missing
# ---------------------------------------------------------------------------


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root lists a folder whatever its mode")
def test_a_folder_that_cant_be_listed_holds_back_every_deletion(env, api, lib):
    for n, rel in enumerate(["locked/a.png", "locked/b.png", "open/c.png", "open/d.png", "open/e.png"]):
        _image(lib, rel, n)
    _scan(api, lib)
    before = _in_sight(env, lib)

    (lib.root / "open/d.png").unlink()
    _move(lib, "open/e.png", "open/sorted/e.png")
    locked = lib.root / "locked"
    locked.chmod(0)
    try:
        _scan(api, lib)
    finally:
        locked.chmod(0o755)

    clips = _in_sight(env, lib)
    assert {"locked/a.png", "locked/b.png", "open/c.png", "open/d.png"} <= set(clips)
    assert _missing(env, lib) == {}
    # A move elsewhere still applies: its old path is no deletion.
    assert clips["open/sorted/e.png"]["asset_id"] == before["open/e.png"]["asset_id"]

    _scan(api, lib)  # listed again: only what's really gone goes missing
    assert set(_missing(env, lib)) == {"open/d.png"}
    assert set(_in_sight(env, lib)) == {"locked/a.png", "locked/b.png", "open/c.png", "open/sorted/e.png"}
    _settled(env, api, lib)


def test_files_still_settling_wait_and_are_never_taken_for_missing(env, api, lib):
    _image(lib, "a.png", 1)
    _scan(api, lib)
    a = _in_sight(env, lib)["a.png"]

    now = time.time()
    _image(lib, "fresh.png", 2, mtime=now)  # just written
    rewritten = _image(lib, "a.png", 3, mtime=now)  # being rewritten
    _scan(api, lib)
    assert _in_sight(env, lib) == {"a.png": a}  # neither missing nor changed yet

    _set_mtime(lib.root / "fresh.png", _ago(60))
    _set_mtime(rewritten, _ago(60))
    _scan(api, lib)
    clips = _in_sight(env, lib)
    assert set(clips) == {"a.png", "fresh.png"}
    assert clips["a.png"]["asset_id"] != a["asset_id"] and clips["a.png"]["content_hash"] == _sha(rewritten)
    _settled(env, api, lib)


def test_a_file_stamped_far_ahead_is_ingested_at_once(env, api, lib):
    # A camera whose clock runs ahead, not a copy in progress.
    _image(lib, "ahead.png", 1, mtime=time.time() + HOUR)
    _scan(api, lib)
    assert set(_in_sight(env, lib)) == {"ahead.png"}
    _settled(env, api, lib)


def test_empty_files_are_never_ingested_nor_taken_for_missing(env, api, lib):
    _image(lib, "a.png", 1)
    _scan(api, lib)
    a = _in_sight(env, lib)["a.png"]

    for rel in ("a.png", "empty.png"):  # a.png being rewritten
        (lib.root / rel).write_bytes(b"")
        _set_mtime(lib.root / rel, _ago(HOUR))
    _scan(api, lib)

    assert _in_sight(env, lib) == {"a.png": a}
    assert len(_rows(env, lib)) == 1
    _settled(env, api, lib)


def test_a_library_whose_folder_isnt_there_changes_nothing(env, api, lib):
    _image(lib, "a.png", 1)
    _scan(api, lib)
    before = _rows(env, lib)

    shutil.rmtree(lib.root)
    _scan(api, lib)
    assert _rows(env, lib) == before


@pytest.mark.parametrize("left", ["nothing", "a file that isn't media"])
def test_an_emptied_library_folder_archives_nothing(env, api, lib, left):
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    before = _rows(env, lib)

    for rel in ("a.png", "b.png"):
        (lib.root / rel).unlink()
    if left != "nothing":
        (lib.root / "notes.txt").write_text("not media")
    _scan(api, lib)
    assert _rows(env, lib) == before  # more likely an unmounted volume than a library emptied


def test_an_emptied_library_is_archived_when_the_scan_is_forced(env, api, lib):
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    for rel in ("a.png", "b.png"):
        (lib.root / rel).unlink()
    (lib.root / "notes.txt").write_text("not media")

    _scan(api, lib, force=True)
    assert _in_sight(env, lib) == {}
    assert set(_missing(env, lib)) == {"a.png", "b.png"}
    _settled(env, api, lib)


def test_the_storage_going_away_during_the_scan_archives_and_moves_nothing(env, api, lib, tmp_path, monkeypatch):
    # A clean unmount leaves an empty folder. Here it goes right after the
    # walk, before the scan has changed anything on the server.
    from src.processing import scan as scan_module

    for n, rel in enumerate(["a.png", "b.png", "c.png"]):
        _image(lib, rel, n)
    _scan(api, lib)
    (lib.root / "a.png").unlink()
    _move(lib, "b.png", "moved/b.png")
    _image(lib, "new.png", 7)
    before = _rows(env, lib)

    walk = scan_module._walk_library
    unmounted = tmp_path / "unmounted"

    def walk_then_unmount(*args, **kwargs):
        found = walk(*args, **kwargs)
        unmounted.mkdir(exist_ok=True)
        for child in list(lib.root.iterdir()):
            shutil.move(str(child), str(unmounted / child.name))
        return found

    monkeypatch.setattr(scan_module, "_walk_library", walk_then_unmount)
    _scan(api, lib)
    assert _rows(env, lib) == before


# ---------------------------------------------------------------------------
# The mass-missing guard: more than 50 clips and more than half of a
# library's clips in sight (src/server/api/routers/assets.py)
# ---------------------------------------------------------------------------


def _seeded_card(env, lib: Lib, count: int) -> None:
    """count clips on the server, each file on disk as the server recorded it."""
    for n in range(count):
        _image(lib, f"card/{n:03}.png", n)
        _seed(env, lib, f"card/{n:03}.png")


def _delete_first(lib: Lib, count: int) -> set[str]:
    gone = {f"card/{n:03}.png" for n in range(count)}
    for rel in gone:
        (lib.root / rel).unlink()
    return gone


def test_most_of_a_library_gone_is_archived_only_when_the_scan_may(env, api, lib):
    _seeded_card(env, lib, 60)
    gone = _delete_first(lib, 51)

    _scan(api, lib)
    assert _missing(env, lib) == {} and len(_in_sight(env, lib)) == 60

    _scan(api, lib, allow_mass_delete=True)
    assert set(_missing(env, lib)) == gone and len(_in_sight(env, lib)) == 9
    _settled(env, api, lib)


def test_fifty_missing_clips_are_archived_without_asking(env, api, lib):
    _seeded_card(env, lib, 60)
    gone = _delete_first(lib, 50)

    _scan(api, lib)
    assert set(_missing(env, lib)) == gone and len(_in_sight(env, lib)) == 10


def test_half_of_a_library_missing_is_archived_without_asking(env, api, lib):
    _seeded_card(env, lib, 102)
    gone = _delete_first(lib, 51)  # more than 50, but not more than half

    _scan(api, lib)
    assert set(_missing(env, lib)) == gone and len(_in_sight(env, lib)) == 51


# ---------------------------------------------------------------------------
# A scan of part of a library
# ---------------------------------------------------------------------------


def test_a_path_prefix_compares_only_that_folder(env, api, lib):
    for n, rel in enumerate(["a/1.png", "a/2.png", "b/3.png"]):
        _image(lib, rel, n)
    _scan(api, lib)
    (lib.root / "a/1.png").unlink()
    (lib.root / "b/3.png").unlink()
    _image(lib, "a/new.png", 7)
    _image(lib, "b/new.png", 8)

    _scan(api, lib, path_prefix="a")

    assert set(_missing(env, lib)) == {"a/1.png"}
    assert set(_in_sight(env, lib)) == {"a/2.png", "a/new.png", "b/3.png"}


def test_a_path_prefix_thats_gone_is_scanned_from_the_nearest_folder_still_there(env, api, lib):
    for n, rel in enumerate(["trip/day1/1.png", "trip/day1/2.png", "trip/day2/3.png", "other/4.png"]):
        _image(lib, rel, n)
    _scan(api, lib)
    shutil.rmtree(lib.root / "trip/day1")
    (lib.root / "other/4.png").unlink()

    _scan(api, lib, path_prefix="trip/day1")

    assert set(_missing(env, lib)) == {"trip/day1/1.png", "trip/day1/2.png"}
    assert set(_in_sight(env, lib)) == {"trip/day2/3.png", "other/4.png"}


def test_a_folder_emptied_and_scanned_by_prefix_has_its_files_gone(env, api, lib):
    # An empty folder within a library is just empty, unlike the library's own.
    for n, rel in enumerate(["a/1.png", "a/2.png", "b/3.png"]):
        _image(lib, rel, n)
    _scan(api, lib)
    for rel in ("a/1.png", "a/2.png"):
        (lib.root / rel).unlink()

    _scan(api, lib, path_prefix="a")

    assert set(_missing(env, lib)) == {"a/1.png", "a/2.png"}
    assert set(_in_sight(env, lib)) == {"b/3.png"}


def test_a_path_prefix_leaving_the_library_changes_nothing(env, api, lib):
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _scan(api, lib)
    (lib.root / "a.png").unlink()
    before = _rows(env, lib)

    _scan(api, lib, path_prefix="../elsewhere")
    assert _rows(env, lib) == before


def test_an_image_only_scan_leaves_videos_alone(env, api, lib):
    _image(lib, "a.png", 1)
    _image(lib, "b.png", 2)
    _video(lib, "old.mp4")
    _seed(env, lib, "old.mp4", media_type="video")
    _scan(api, lib)
    (lib.root / "a.png").unlink()
    (lib.root / "old.mp4").unlink()
    _video(lib, "new.mp4")

    _scan(api, lib, media_type_filter="image")

    assert set(_missing(env, lib)) == {"a.png"}
    assert set(_in_sight(env, lib)) == {"b.png", "old.mp4"}


# ---------------------------------------------------------------------------
# Dry runs, and what the walk takes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("allow_moves", [False, True])
def test_a_dry_run_changes_nothing_on_the_server(env, api, lib, allow_moves):
    for n, rel in enumerate(["keep.png", "change.png", "gone.png", "move.png", "touch.png"]):
        _image(lib, rel, n, mtime=_ago(2 * HOUR))
    _scan(api, lib)
    _image(lib, "change.png", 10)
    (lib.root / "gone.png").unlink()
    _move(lib, "move.png", "moved/move.png")
    _set_mtime(lib.root / "touch.png", _ago(HOUR))
    _image(lib, "new.png", 11)
    before = _rows(env, lib)

    _scan(api, lib, dry_run=True, allow_moves=allow_moves)
    assert _rows(env, lib) == before


def test_what_the_walk_takes(env, api, lib):
    import unicodedata

    _image(lib, ".hidden.png", 1)  # hidden files are listed
    (lib.root / "notes.txt").write_text("not media")
    (lib.root / "link.png").symlink_to(_image(lib, "real.png", 2))  # symlinks aren't followed
    _image(lib, unicodedata.normalize("NFD", "café.png"), 3)  # names as NFC, as the Mac app stores them
    _image(lib, "bad\nname.png", 4)  # a control character: the server refuses the name
    _scan(api, lib)

    assert set(_in_sight(env, lib)) == {".hidden.png", "real.png", unicodedata.normalize("NFC", "café.png")}
    _settled(env, api, lib)
