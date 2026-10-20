# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Files go with their rows (ADR-020 phase 0): nothing Lumiverb stored is
left behind when what it was of is deleted.

- a clip deleted for good takes its scenes' frames and its faces' crops,
  besides its previews (routers/trash.py purge_assets);
- a face a re-detection drops takes its crop (FaceRepository.submit_faces,
  after the commit);
- the daily sweep looks at face crops too (search/cleanup.py).

Through the real API and database, files on a real previews folder. A
library deleted for good: tests/test_library_previews_api.py.
"""

from __future__ import annotations

import io
import os
import time

import pytest
from PIL import Image
from sqlalchemy import text

from tests.faces import plant_faces
from tests.scenes import Scene, described_scenes
from tests.test_analysis_proxy_api import _ingest, env  # noqa: F401 — the shared server fixture
from tests.test_archive_by_hand import _db, _sha

pytestmark = pytest.mark.slow

BOX_A = {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.3}
BOX_B = {"x": 0.6, "y": 0.2, "w": 0.2, "h": 0.3}


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), (90, 80, 70)).save(buf, format="JPEG")
    return buf.getvalue()


def _face(box: dict, seed: float) -> dict:
    return {"bounding_box": box, "detection_confidence": 0.99, "embedding": [seed] + [0.0] * 511}


def _crops(env, asset_id: str) -> dict[str, str]:
    """The clip's faces' crop keys, by face id."""
    with _db(env) as session:
        return dict(session.execute(text("SELECT face_id, crop_key FROM faces WHERE asset_id = :a"),
                                    {"a": asset_id}).all())


def _there(env, key: str) -> bool:
    storage = env[3]
    return any(p.exists() for p in storage.paths(key))


def _scene_frames(env, asset_id: str, scene_starts: list[int]) -> list[str]:
    """Each scene's frame, uploaded as the scenes producer does: their keys."""
    client, headers, library_id, storage, tenant_id, _ = env
    keys = []
    for ms in scene_starts:
        r = client.post(f"/v1/assets/{asset_id}/artifacts/lumiverb.video.scenes.picture", data={"rep_frame_ms": str(ms)},
                        files={"file": ("frame.jpg", io.BytesIO(_jpeg()), "image/jpeg")}, headers=headers)
        assert r.status_code == 200, r.text
        keys.append(storage.scene_rep_key(tenant_id, library_id, asset_id, ms))
    return keys


def test_a_clip_deleted_for_good_takes_its_scene_frames_and_face_crops(env):
    client, headers, *_ = env
    video = _ingest(env, "leaks/purge.mov", sha=_sha())
    described_scenes(client, headers, video, [Scene(0, 4000, "a beach"), Scene(31000, 35000, "a dog")])
    frames = _scene_frames(env, video, [0, 31000])
    photo = _ingest(env, "leaks/purge.jpg", media_type="image", sha=_sha())
    plant_faces(client, photo, [_face(BOX_A, 1.0), _face(BOX_B, -1.0)], headers=headers)
    crops = list(_crops(env, photo).values())
    with _db(env) as session:
        files = list(session.execute(text(
            "SELECT unnest(ARRAY[proxy_key, thumbnail_key]) FROM assets WHERE asset_id = ANY(:ids)"),
            {"ids": [video, photo]}).scalars())
    assert len(crops) == 2 and all(crops) and all(_there(env, k) for k in [*frames, *crops, *files])

    for asset_id in (video, photo):
        assert client.delete(f"/v1/assets/{asset_id}", headers=headers).status_code in (200, 204)
    r = client.request("DELETE", "/v1/trash/empty", json={"asset_ids": [video, photo]}, headers=headers)
    assert r.status_code == 200 and r.json()["deleted"] == 2, r.text

    assert [k for k in [*frames, *crops, *files] if _there(env, k)] == []


def test_a_face_a_redetection_drops_takes_its_crop(env):
    client, headers, *_ = env
    photo = _ingest(env, "leaks/redetect.jpg", media_type="image", sha=_sha())
    kept, dropped = plant_faces(client, photo, [_face(BOX_A, 1.0), _face(BOX_B, -1.0)], headers=headers)
    crops = _crops(env, photo)
    assert _there(env, crops[kept]) and _there(env, crops[dropped])

    assert plant_faces(client, photo, [_face(BOX_A, 1.0)], headers=headers) == [kept]  # B isn't found again

    assert _crops(env, photo) == {kept: crops[kept]}
    assert _there(env, crops[kept]) and not _there(env, crops[dropped])


def test_the_daily_sweep_takes_crops_no_face_has(env):
    from src.server.search.cleanup import run_cleanup_for_tenant

    client, headers, library_id, storage, tenant_id, _ = env
    photo = _ingest(env, "leaks/sweep.jpg", media_type="image", sha=_sha())
    [face] = plant_faces(client, photo, [_face(BOX_A, 1.0)], headers=headers)
    crop = _crops(env, photo)[face]
    # Enough of the library's own that the stray one is well under the sweep's 25% cap.
    for n in range(8):
        _ingest(env, f"leaks/sweep-{n}.jpg", media_type="image", sha=_sha())
    stray = storage.face_crop_key(tenant_id, library_id, "face_01HZZZZZZZZZZZZZZZZZZZZZZZ")
    path = storage.write_path(stray)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"left by a face long gone")
    an_hour_ago = time.time() - 7200  # the sweep leaves anything newer than an hour
    for key in (stray, crop):
        for p in storage.paths(key):
            if p.exists():
                os.utime(p, (an_hour_ago, an_hour_ago))

    with _db(env) as session:
        result = run_cleanup_for_tenant(tenant_id, session, dry_run=False, library_id=library_id)
    assert result.errors == [] and result.skipped_libraries == 0
    assert not _there(env, stray) and _there(env, crop)
