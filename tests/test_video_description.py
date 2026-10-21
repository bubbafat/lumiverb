"""A video's own description (src/producers/video_description), against the real database: due once its
scenes are described, saved where a photo's description is (POST /v1/assets/batch-vision, under its own
producer), shown as the clip's description and found by search; never a photo's, nor a video's under the
photos' producer; made again with its scenes' descriptions."""

from __future__ import annotations

import pytest

from src.producers.names import SCENE_DESCRIPTIONS, VIDEO_DESCRIPTION
from src.shared.producers import PRODUCERS
from tests.clips import make_clip
from tests.machine_lineage import made
from tests.producer_fakes import due
from tests.scenes import Scene, described_scenes
from tests.test_scans_api import env  # noqa: F401 — the shared fixture

pytestmark = pytest.mark.slow

WRITTEN = "Gulls wheel over a quiet harbour, then a ferry pulls away from the pier at dusk."


@pytest.fixture
def library(env):  # noqa: F811
    r = env.client.post("/v1/libraries", json={"name": "Harbour", "root_path": "/Volumes/harbour"},
                        headers=env.admin)
    assert r.status_code == 200, r.text
    return r.json()["library_id"]


def _save(env, asset_id: str, producer: str = VIDEO_DESCRIPTION):  # noqa: F811
    return env.client.post("/v1/assets/batch-vision", headers=env.admin, json={
        "items": [{"asset_id": asset_id, "model_id": "test-vision", "description": WRITTEN,
                   "tags": ["harbour", "ferry"]}],
        "lineage": made(producer)})


def test_a_video_is_described_from_its_scenes_shown_and_searched(env, library):  # noqa: F811
    video = make_clip(env.tenant_url, library, "harbour.mov", media_type="video")
    photo = make_clip(env.tenant_url, library, "harbour.jpg")
    assert due(env.tenant_url, VIDEO_DESCRIPTION, [library]) == [], "no scenes described yet"

    described_scenes(env.client, env.admin, video, [Scene(0, 4000, "Gulls over a harbour."),
                                                     Scene(4000, 9000, "A ferry leaves the pier at dusk.")])
    assert [c["asset_id"] for c in due(env.tenant_url, VIDEO_DESCRIPTION, [library])] == [video]

    r = _save(env, video)
    assert r.status_code == 200 and r.json() == {"updated": 1, "skipped": 0}, r.text
    assert due(env.tenant_url, VIDEO_DESCRIPTION, [library]) == []
    detail = env.client.get(f"/v1/assets/{video}", headers=env.admin).json()
    assert (detail["ai_description"], detail["ai_tags"]) == (WRITTEN, ["harbour", "ferry"])
    r = env.client.get("/v1/query", params={"f": ["query:ferry", f"library:{library}"]}, headers=env.admin)
    assert r.status_code == 200, r.text
    assert video in [i["asset_id"] for i in r.json()["items"]]

    # Each producer describes its own kind of clip.
    for clip, producer in ((photo, VIDEO_DESCRIPTION), (video, "lumiverb.image.description")):
        r = _save(env, clip, producer)
        assert (r.status_code, r.json()["error"]["code"]) == (422, "wrong_media_type"), r.text


def test_it_is_made_again_with_its_scenes_descriptions():
    assert VIDEO_DESCRIPTION in PRODUCERS[SCENE_DESCRIPTIONS].redo_also
    assert PRODUCERS[VIDEO_DESCRIPTION].needs == (SCENE_DESCRIPTIONS,)
