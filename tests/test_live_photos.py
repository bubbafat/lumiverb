# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Live Photos: an iPhone's photo and the few seconds of video beside it are one clip, the photo
(src/server/repository/live_photos.py). End to end: the real scan over real files, the real API and
its database, the Live Photos producer for clips scanned before.

tests/fixtures/live.heic is tests/fixtures/drawn.heic with a minimal Apple maker note holding only a
content identifier, and tests/fixtures/live.mov a 1.5-second H.264 clip whose QuickTime keys hold the
same one (exiftool wrote both, as an iPhone does), so neither is anyone's photo.
"""

from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import text

from src.processing.workers.exif_extract import extract_exif, live_photo_id
from src.shared.live_photos import identifier
from tests.machine_lineage import made
from tests.producer_fakes import FakeAccount, due, run_job, scheduler_client
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_archive_by_hand import _db
from tests.test_scan_outcomes import (  # noqa: F401 — fixtures
    HOUR,
    Lib,
    _ago,
    _cache_here,
    _in_sight,
    _rows,
    _scan,
    _set_mtime,
    _settled,
    _video,
    api,
    lib,
)
from tests.videos import playable

FIXTURES = Path(__file__).parent / "fixtures"
ID = "1F2E3D4C-5B6A-4798-8A9B-0C1D2E3F4A5B"


def _put(lib: Lib, fixture: str, rel: str) -> Path:
    path = lib.root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / fixture, path)
    _set_mtime(path, _ago(HOUR))
    return path


def _by_path(env, lib: Lib) -> dict[str, dict]:
    with _db(env) as session:
        rows = session.execute(text(
            "SELECT rel_path, asset_id, live_photo_id, deleted_reason, video_preview_key FROM assets"
            " WHERE library_id = :lib"), {"lib": lib.id}).mappings().all()
    return {r["rel_path"]: dict(r) for r in rows}


# ---------------------------------------------------------------------------
# What a file says
# ---------------------------------------------------------------------------


def test_an_identifier_is_kept_uppercase_and_anything_else_is_none() -> None:
    assert identifier(" 1f2e3d4c-5b6a-4798-8a9b-0c1d2e3f4a5b ") == ID
    for raw in (None, "", 7, "not one!", "x" * 65, "a;b"):
        assert identifier(raw) is None, raw


def test_exiftool_reads_the_same_identifier_in_the_photo_and_its_video() -> None:
    assert live_photo_id(extract_exif(FIXTURES / "live.heic")) == ID
    assert live_photo_id(extract_exif(FIXTURES / "live.mov")) == ID
    assert live_photo_id(extract_exif(FIXTURES / "drawn.heic")) is None


# ---------------------------------------------------------------------------
# A scan
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_live_photo_is_its_photo_and_a_name_shared_means_nothing(env, api, lib):
    _put(lib, "live.heic", "trip/IMG_0001.HEIC")
    _put(lib, "live.mov", "trip/IMG_0001.MOV")
    _put(lib, "drawn.heic", "trip/IMG_0002.HEIC")  # a still and a video of its name, no identifier
    _video(lib, "trip/IMG_0002.mp4")
    _scan(api, lib)

    clips = _by_path(env, lib)
    photo, motion = clips["trip/IMG_0001.HEIC"], clips["trip/IMG_0001.MOV"]
    assert (photo["live_photo_id"], photo["deleted_reason"]) == (ID, None)
    assert (motion["live_photo_id"], motion["deleted_reason"]) == (ID, "live_photo")
    assert motion["video_preview_key"], "its preview arrived before it was paired"
    assert set(_in_sight(env, lib)) == {"trip/IMG_0001.HEIC", "trip/IMG_0002.HEIC", "trip/IMG_0002.mp4"}

    client, headers, *_ = env
    r = client.get("/v1/query", params={"f": f"library:{lib.id}"}, headers=headers)
    assert sorted(i["rel_path"] for i in r.json()["items"]) == [
        "trip/IMG_0001.HEIC", "trip/IMG_0002.HEIC", "trip/IMG_0002.mp4"]
    assert client.get(f"/v1/assets/{photo['asset_id']}", headers=headers).json()["live_photo"] is True
    assert client.get(f"/v1/assets/{clips['trip/IMG_0002.HEIC']['asset_id']}",
                      headers=headers).json()["live_photo"] is False
    # Scanned again, the motion is the same file: skipped, never a new clip, and no one removed it.
    stats = _scan(api, lib)
    assert stats.unreadable == []
    _settled(env, api, lib)


@pytest.mark.slow
def test_its_video_scanned_first_is_paired_once_the_photo_is(env, api, lib):
    _put(lib, "live.mov", "IMG_0003.MOV")
    _scan(api, lib)
    assert set(_in_sight(env, lib)) == {"IMG_0003.MOV"}
    _put(lib, "live.heic", "IMG_0003.HEIC")
    _scan(api, lib)
    assert set(_in_sight(env, lib)) == {"IMG_0003.HEIC"}
    assert _by_path(env, lib)["IMG_0003.MOV"]["deleted_reason"] == "live_photo"
    _settled(env, api, lib)


@pytest.mark.slow
def test_force_add_never_brings_a_motion_back_on_its_own(env, api, lib):
    _put(lib, "live.heic", "IMG_0004.HEIC")
    _put(lib, "live.mov", "IMG_0004.MOV")
    _scan(api, lib)
    before = _rows(env, lib)
    _scan(api, lib, force_add=True)
    assert _rows(env, lib) == before


@pytest.mark.slow
def test_the_photo_plays_its_motion_and_any_other_photo_plays_nothing(env, api, lib):
    _put(lib, "live.heic", "IMG_0005.HEIC")
    _put(lib, "live.mov", "IMG_0005.MOV")
    _put(lib, "drawn.heic", "still.heic")
    _scan(api, lib)
    clips = _by_path(env, lib)
    client, headers, *_ = env
    r = client.get(f"/v1/assets/{clips['IMG_0005.HEIC']['asset_id']}/playback", headers=headers)
    assert r.status_code == 200, r.text
    played = client.get(r.json()["url"])
    assert played.status_code == 200 and played.headers["content-type"] == "video/mp4"
    assert played.content[4:8] == b"ftyp" and len(played.content) > 1000
    r = client.get(f"/v1/assets/{clips['still.heic']['asset_id']}/playback", headers=headers)
    assert r.status_code == 422
    # The motion itself is no clip of its own to play.
    assert client.get(f"/v1/assets/{clips['IMG_0005.MOV']['asset_id']}/playback", headers=headers).status_code == 404


@pytest.mark.slow
def test_the_photo_trashed_and_restored_keeps_its_motion_and_deleted_for_good_takes_it(env, api, lib):
    _put(lib, "live.heic", "IMG_0006.HEIC")
    _put(lib, "live.mov", "IMG_0006.MOV")
    _scan(api, lib)
    photo = _by_path(env, lib)["IMG_0006.HEIC"]["asset_id"]
    client, headers, *_ = env
    assert client.delete(f"/v1/assets/{photo}", headers=headers).status_code in (200, 204)
    _scan(api, lib)
    assert _by_path(env, lib)["IMG_0006.MOV"]["deleted_reason"] == "live_photo"
    r = client.post(f"/v1/assets/{photo}/restore", headers=headers)
    assert r.status_code == 204, r.text
    assert client.get(f"/v1/assets/{photo}", headers=headers).json()["live_photo"] is True

    assert client.delete(f"/v1/assets/{photo}", headers=headers).status_code in (200, 204)
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [photo]}, headers=headers)
    assert r.status_code == 200, r.text
    assert _rows(env, lib) == []
    # Both files are still on disk: neither comes back.
    _scan(api, lib)
    assert _rows(env, lib) == []


@pytest.mark.slow
def test_one_photo_deleted_for_good_leaves_the_motion_to_another_photo_of_it(env, api, lib):
    _put(lib, "live.heic", "IMG_0007.HEIC")
    _put(lib, "live.heic", "copy/IMG_0007.HEIC")
    _put(lib, "live.mov", "IMG_0007.MOV")
    _scan(api, lib)
    clips = _by_path(env, lib)
    client, headers, *_ = env
    assert client.delete(f"/v1/assets/{clips['IMG_0007.HEIC']['asset_id']}", headers=headers).status_code in (200, 204)
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [clips["IMG_0007.HEIC"]["asset_id"]]},
                       headers=headers)
    assert r.status_code == 200, r.text
    assert _by_path(env, lib)["IMG_0007.MOV"]["deleted_reason"] == "live_photo"
    assert client.get(f"/v1/assets/{clips['copy/IMG_0007.HEIC']['asset_id']}",
                      headers=headers).json()["live_photo"] is True


# ---------------------------------------------------------------------------
# Clips scanned before: the Live Photos producer
# ---------------------------------------------------------------------------


def _ingest(env, lib: Lib, rel: str, media_type: str) -> str:
    """A clip as a scan from before Live Photos left it: its camera's make, no identifier read."""
    client, headers, *_ = env
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, format="JPEG")
    buf.seek(0)
    r = client.post("/v1/ingest", headers=headers, files={"lumiverb.preview": ("p.jpg", buf, "image/jpeg")}, data={
        "library_id": lib.id, "rel_path": rel, "file_size": "10", "media_type": media_type,
        "lineage": json.dumps({"lumiverb.preview": made("lumiverb.preview")}),
        "exif": json.dumps({"content_hash": "sha256:" + os.urandom(32).hex(), "camera_make": "Apple"})})
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


@pytest.mark.slow
def test_the_producer_reads_clips_beside_their_other_half_and_pairs_them(env, lib):
    from src.producers.live_photo.work import LivePhoto

    _put(lib, "live.heic", "old/IMG_0008.HEIC")
    _put(lib, "live.mov", "old/IMG_0008.MOV")
    _put(lib, "drawn.heic", "old/alone.heic")
    photo = _ingest(env, lib, "old/IMG_0008.HEIC", "image")
    motion = _ingest(env, lib, "old/IMG_0008.MOV", "video")
    client, headers, *_ = env
    r = client.post(f"/v1/assets/{motion}/artifacts/lumiverb.video.preview", headers=headers,
                    files={"file": ("preview.mp4", playable(), "video/mp4")},
                    data={"lineage": json.dumps(made("lumiverb.video.preview"))})
    assert r.status_code == 200, r.text
    _ingest(env, lib, "old/alone.heic", "image")  # no file of the other kind beside it: never read

    items = due(env[-1], "lumiverb.live_photo", [lib.id])
    assert {c["asset_id"] for c in items} == {photo, motion}
    acct = FakeAccount(scheduler_client(env[4]), {lib.id: lib.root})
    assert run_job(LivePhoto, acct, env[4], "lumiverb.live_photo", items) is None  # every clip done
    assert acct.failures.charged == []
    clips = _by_path(env, lib)
    assert (clips["old/IMG_0008.HEIC"]["live_photo_id"], clips["old/IMG_0008.HEIC"]["deleted_reason"]) == (ID, None)
    assert (clips["old/IMG_0008.MOV"]["live_photo_id"], clips["old/IMG_0008.MOV"]["deleted_reason"]) == (
        ID, "live_photo")
    assert due(env[-1], "lumiverb.live_photo", [lib.id]) == []


@pytest.mark.slow
def test_the_route_takes_only_an_identifier_and_says_how_it_was_read(env, lib):
    client, headers, *_ = env
    a = _ingest(env, lib, "route.heic", "image")
    r = client.put(f"/v1/assets/{a}/live-photo", json={"live_photo_id": ID}, headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "lineage_required"
    r = client.put(f"/v1/assets/{a}/live-photo", json={"live_photo_id": "no way!", "lineage": made("lumiverb.live_photo")},
                   headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_an_identifier"
    r = client.put(f"/v1/assets/{a}/live-photo", json={"live_photo_id": None, "lineage": made("lumiverb.live_photo")},
                   headers=headers)
    assert r.status_code == 200 and r.json() == {"hidden": 0}
    r = client.put("/v1/assets/ast_nope/live-photo", json={"lineage": made("lumiverb.live_photo")}, headers=headers)
    assert r.status_code == 404
