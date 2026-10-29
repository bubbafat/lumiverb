# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Visual search in videos: each scene's picture's CLIP vector
(src/producers/scene_likeness), made with the model photos' are.

The producer reads each scene's frame from the video's analysis proxy, has
the CLIP job's machines embed it, and saves the vectors a few scenes at a
time (POST /v1/video/{id}/scene-likeness). The video is made once every one
of its scenes has its vector, and only then is its lineage recorded; a scene
gone meanwhile is passed by. A redo keeping a frame keeps its vector:
tests/test_scene_redo_api.py.
"""

from __future__ import annotations

import io
import json
import math

import pytest
from PIL import Image
from sqlalchemy import text

from src.producers.clip import CLIP_MODEL_ID
from src.producers.pools import CLIP_JOB
from tests.producer_fakes import FakeAccount, due, run_job, scheduler_client
from tests.scenes import Scene, described_scenes
from tests.test_analysis_proxy_api import _upload, env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _row, _sha, _want
from tests.test_reconciler import _counts, _library
from tests.videos import playable

pytestmark = pytest.mark.slow

LIKENESS = "lumiverb.video.scene_likeness"
MODEL = "ViT-B-32-openai"  # the CLIP job's model and weights, as photos' vectors are made with
VECTOR = [round(math.sin(i) / 20, 6) for i in range(512)]  # what a stand-in machine makes of every picture


def _video(lib, scenes: list[Scene], name: str = "v.mov") -> tuple[str, str, list[str]]:
    """A video found in these scenes (each scene's picture at its start), not embedded: its id, content hash and
    scene ids."""
    client, headers, *_ = lib
    sha = _sha()
    vid = _ingest_with(lib, name, sha, None, media_type="video")
    return vid, sha, described_scenes(client, headers, vid, scenes)


def _save(lib, vid: str, sha: str, scene_ids: list[str], vector: list[float] = VECTOR, **body):
    """POST the scenes' vectors as the producer does."""
    client, headers, *_ = lib
    return client.post(f"/v1/video/{vid}/scene-likeness", headers=headers, json={
        "scenes": [{"scene_id": s, "vector": vector} for s in scene_ids], "model_id": CLIP_MODEL_ID,
        "model_version": MODEL, "lineage": _want(lib, LIKENESS, sha), **body})


def _listed(lib, vid: str) -> list[str | None]:
    client, headers, *_ = lib
    r = client.get(f"/v1/video/{vid}/scenes", headers=headers)
    assert r.status_code == 200, r.text
    assert all("likeness" not in s for s in r.json()["scenes"]), "the vector itself isn't listed"
    return [s["likeness_model"] for s in r.json()["scenes"]]


def _vectors(lib, vid: str) -> list[tuple[list[float] | None, str | None]]:
    """Each scene's vector and model, as kept, in order."""
    with _db(lib) as s:
        rows = s.execute(text("SELECT CAST(likeness AS text), likeness_model FROM video_scenes WHERE asset_id = :a"
                              " ORDER BY start_ms"), {"a": vid}).all()
    return [([float(x) for x in v.strip("[]").split(",")] if v else None, m) for v, m in rows]


def _same(vector: list[float] | None, want: list[float]) -> bool:
    """As kept (pgvector keeps 4-byte floats)."""
    return (vector is not None and len(vector) == len(want)
            and all(abs(a - b) < 1e-6 for a, b in zip(vector, want, strict=True)))


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


def test_vectors_are_kept_and_the_video_is_made_once_every_scene_has_one(env):
    lib = _library(env, "SceneLikeness")
    vid, sha, (first, second) = _video(lib, [Scene(0, 1000, "a dog"), Scene(1000, 2000, "a cat")])
    assert _listed(lib, vid) == [None, None]
    assert _counts(lib, LIKENESS)["missing"] == 1

    r = _save(lib, vid, sha, [first])
    assert r.status_code == 200, r.text
    assert r.json() == {"saved": 1, "gone": 0, "made": False}
    # Made in parts: until every scene has its vector the video is missing them, and nothing says how they were made.
    assert _listed(lib, vid) == [MODEL, None]
    assert _counts(lib, LIKENESS)["missing"] == 1 and _row(lib, vid, LIKENESS) is None

    r = _save(lib, vid, sha, [second], vector=[0.5] * 512)
    assert r.json() == {"saved": 1, "gone": 0, "made": True}
    kept = _vectors(lib, vid)
    assert _same(kept[0][0], VECTOR) and _same(kept[1][0], [0.5] * 512) and [m for _, m in kept] == [MODEL, MODEL]
    c = _counts(lib, LIKENESS)
    assert (c["current"], c["missing"], c["stale"]) == (1, 0, 0)
    assert _row(lib, vid, LIKENESS)[:4] == (LIKENESS, "1", _want(lib, LIKENESS, sha)["settings_hash"], sha)


def test_a_scene_not_the_videos_is_passed_by_not_refused(env):
    lib = _library(env, "SceneLikenessGone")
    vid, sha, (scene,) = _video(lib, [Scene(0, 1000, "a dog")])
    other, _, (elsewhere,) = _video(lib, [Scene(0, 1000, "a cat")], name="w.mov")
    r = _save(lib, vid, sha, ["scn_gone", elsewhere])  # dropped meanwhile, and another video's
    assert r.status_code == 200, r.text
    assert r.json() == {"saved": 0, "gone": 2, "made": False}
    assert _vectors(lib, other) == [(None, None)]
    assert _save(lib, vid, sha, [scene, "scn_gone"]).json() == {"saved": 1, "gone": 1, "made": True}


def test_a_save_must_say_how_it_was_made_and_whose(env):
    lib = _library(env, "SceneLikenessSays")
    client, headers, *_ = lib
    vid, sha, (scene,) = _video(lib, [Scene(0, 1000, "a dog")])
    for lineage, code in ((None, "lineage_required"), (_want(lib, "lumiverb.image.likeness", sha),
                                                         "lineage_wrong_producer")):
        r = _save(lib, vid, sha, [scene], lineage=lineage)
        assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text
    r = _save(lib, vid, sha, [scene], model_id="FeaturePrint")
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_clip", r.text
    r = client.post(f"/v1/video/{vid}/scene-likeness", headers=headers, json={
        "scenes": [{"scene_id": scene, "vector": [0.1] * 512}, {"scene_id": scene, "vector": [0.1] * 768}],
        "model_id": CLIP_MODEL_ID, "model_version": MODEL, "lineage": _want(lib, LIKENESS, sha)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "vectors_differ", r.text
    r = client.post(f"/v1/video/{vid}/scene-likeness", headers={**headers, "content-type": "application/json"},
                    content=json.dumps({"scenes": [{"scene_id": scene, "vector": [float("nan")] * 512}],
                                        "model_id": CLIP_MODEL_ID, "model_version": MODEL,
                                        "lineage": _want(lib, LIKENESS, sha)}))  # Python's JSON writes NaN
    assert r.status_code == 422, r.text  # numbers, every one finite
    photo = _ingest_with(lib, "p.jpg", _sha())
    r = _save(lib, photo, sha, [scene])
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_a_video", r.text
    assert _vectors(lib, vid) == [(None, None)] and _row(lib, vid, LIKENESS) is None  # nothing was kept


def test_only_an_editor_saves_them(env):
    from tests.test_archive_trash_safety import _key_with_role

    lib = _library(env, "SceneLikenessWho")
    client, *_ = lib
    vid, sha, (scene,) = _video(lib, [Scene(0, 1000, "a dog")])
    body = {"scenes": [{"scene_id": scene, "vector": VECTOR}], "model_id": CLIP_MODEL_ID, "model_version": MODEL,
            "lineage": _want(lib, LIKENESS, sha)}
    r = client.post(f"/v1/video/{vid}/scene-likeness", json=body, headers=_key_with_role(lib, "viewer"))
    assert r.status_code == 403, r.text
    assert _vectors(lib, vid) == [(None, None)]
    r = client.post(f"/v1/video/{vid}/scene-likeness", json=body, headers=_key_with_role(lib, "editor"))
    assert r.status_code == 200 and r.json()["made"] is True, r.text


# ---------------------------------------------------------------------------
# The producer's work, against a real analysis proxy
# ---------------------------------------------------------------------------


class _Recording:
    """The scheduler's client, keeping what's posted."""

    def __init__(self, client) -> None:
        self._client = client
        self.posted: list[tuple[str, dict]] = []

    def __getattr__(self, name: str):
        return getattr(self._client, name)

    def post(self, path: str, **kwargs):
        self.posted.append((path, kwargs.get("json")))
        return self._client.post(path, **kwargs)


class _Machine:
    """The CLIP job's machines, as its guard hands them out: a stand-in that makes VECTOR of every picture, saying
    the model in use (src/processing/clip_guard.py BuiltInClip's answer)."""

    def __init__(self) -> None:
        self.pictures: list[bytes] = []

    def embed(self, image: bytes) -> dict:
        self.pictures.append(image)
        return {"vector": VECTOR, "model_id": CLIP_MODEL_ID, "model_version": MODEL}


class _Guard:
    """The CLIP job's guard: its machines, and whose a failure is (a machine checks out fine: the clip's)."""

    def __init__(self) -> None:
        self.machine = _Machine()
        self.charged: list[str] = []

    def embedder(self, models):
        return self.machine

    def charges(self, error) -> bool:
        self.charged.append(str(error))
        return True


def _account(env) -> tuple[FakeAccount, _Guard]:
    """The scheduler's account beside the API: its client, and the analysis proxy read from the Store by key."""
    from src.server.scheduler.inputs import Inputs
    from src.server.storage.store import get_store

    acct = FakeAccount(_Recording(scheduler_client(env[4])))
    acct.inputs = Inputs(acct, get_store())
    acct.models = None  # the built-in machine's (the scheduler's models): the stand-in needs none
    guard = _Guard()
    acct.guard = {CLIP_JOB.name: guard}.__getitem__
    return acct, guard


def _proxied(lib, scenes: list[Scene], seconds: float = 3.0) -> tuple[str, str, list[str]]:
    vid, sha, ids = _video(lib, scenes)
    r = _upload(lib, vid, playable(seconds))
    assert r.status_code == 200, r.text
    return vid, sha, ids


def test_each_scenes_picture_is_read_from_the_analysis_proxy_and_embedded(env):
    from src.producers.scene_likeness.work import SceneLikeness

    lib = _library(env, "SceneLikenessWork")
    vid, sha, ids = _proxied(lib, [Scene(0, 1000, "a"), Scene(1000, 2000, "b"), Scene(2000, 3000, "c")])
    items = due(env[-1], LIKENESS, [lib[2]])
    assert [i["asset_id"] for i in items] == [vid]

    acct, guard = _account(env)
    assert run_job(SceneLikeness, acct, env[4], LIKENESS, items) is None  # made
    assert acct.failures.charged == [] and guard.charged == []
    # Each scene's picture, a JPEG of the frame at its rep_frame_ms (testsrc2 moves: each its own).
    assert len(guard.machine.pictures) == 3 and len(set(guard.machine.pictures)) == 3
    for picture in guard.machine.pictures:
        with Image.open(io.BytesIO(picture)) as img:
            assert img.format == "JPEG" and img.size == (160, 120)
    (path, body), = acct.client.posted
    assert path == f"/v1/video/{vid}/scene-likeness"
    assert [i["scene_id"] for i in body["scenes"]] == ids and all(i["vector"] == VECTOR for i in body["scenes"])
    assert (body["model_id"], body["model_version"]) == (CLIP_MODEL_ID, MODEL)
    assert body["lineage"] == _want(lib, LIKENESS, sha)  # the account's settings, as the server says them
    assert all(_same(v, VECTOR) and m == MODEL for v, m in _vectors(lib, vid))
    assert _counts(lib, LIKENESS)["current"] == 1 and due(env[-1], LIKENESS, [lib[2]]) == []

    # Handed out again: every scene has a vector of the model in use, so nothing is embedded or sent.
    acct, guard = _account(env)
    assert run_job(SceneLikeness, acct, env[4], LIKENESS, items) is None
    assert guard.machine.pictures == [] and acct.client.posted == []
    # Its redo (made another way than now): a scene's vector can't say how, so each is made again.
    assert run_job(SceneLikeness, acct, env[4], LIKENESS, [{**items[0], "redo": True}]) is None
    assert len(guard.machine.pictures) == 3 and len(acct.client.posted) == 1


def test_a_scene_with_no_frame_is_the_videos_once_the_rest_are_saved(env):
    from src.producers.scene_likeness.work import SceneLikeness

    lib = _library(env, "SceneLikenessNoFrame")
    # A scene whose picture is past the analysis proxy's end: ffmpeg finds no frame there.
    vid, sha, ids = _proxied(lib, [Scene(0, 1000, "a"), Scene(1000, 2000, "b"), Scene(60000, 61000, "c")])
    acct, guard = _account(env)
    assert run_job(SceneLikeness, acct, env[4], LIKENESS, due(env[-1], LIKENESS, [lib[2]])) is None
    assert [(a, v) for a, v, _ in acct.failures.charged] == [(LIKENESS, vid)]
    assert "1 of 3 scenes failed: no frame at 60000 ms" in acct.failures.charged[0][2]
    assert [m for _, m in _vectors(lib, vid)] == [MODEL, MODEL, None]  # the rest are kept
    assert _counts(lib, LIKENESS)["missing"] == 1 and _row(lib, vid, LIKENESS) is None
