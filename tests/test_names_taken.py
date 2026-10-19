# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The server takes the names (ADR-024 phase 1 part 1): every route sent an
artifact, a lineage's producer or a file's kind takes its name too
(src/producers/names.py), with the same answer, and keeps today's id. What's
never renamed (AI jobs, model ids, protocol 1's inputs) isn't taken by a name.

Against the real API and database (tests/test_analysis_proxy_api.py's
account). A device's gate takes them too: tests/test_device_keys.py.
"""

from __future__ import annotations

import hashlib
import io
import json

import pytest
from PIL import Image
from sqlalchemy import create_engine, inspect

from src.producers.names import ARTIFACTS, FILES, makers
from tests.derived import saved
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _ingest_with, _row, _sha, _state, _want
from tests.test_reconciler import _counts, _due, _library
from tests.videos import playable

pytestmark = pytest.mark.slow


def _named(env, artifact: str, sha: str | None) -> dict:
    """Lineage as a current worker would send it, its producer by the name of what it makes."""
    return {**_want(env, artifact, sha), "producer": ARTIFACTS[artifact]}


def _jpeg() -> tuple:
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color=(40, 50, 60)).save(buf, format="JPEG")
    return ("p.jpg", io.BytesIO(buf.getvalue()), "image/jpeg")


def test_each_name_is_stored_where_it_says(env):
    # What docs/data-names.md says each is kept in: the account's tables and columns, as migrated.
    engine = create_engine(env[-1])
    try:
        db = inspect(engine)
        tables = set(db.get_table_names())
        for d in makers().values():
            for s in d.stored:
                table, _, column = s.partition(".")
                assert table in tables, (d.name, s)
                assert not column or column in {c["name"] for c in db.get_columns(table)}, (d.name, s)
    finally:
        engine.dispose()


def test_a_clip_ingested_by_names_is_as_one_by_ids(env):
    lib = _library(env, "NamesIngest")
    client, headers, library_id, *_ = lib
    sha = _sha()
    data = {"library_id": library_id, "rel_path": "named.jpg", "file_size": "1000", "media_type": "image",
            "width": "64", "height": "36", "exif": json.dumps({"content_hash": sha}),
            "lineage": json.dumps({"lumiverb.preview": _named(lib, "proxy", sha)})}
    r = client.post("/v1/ingest", data=data, files={"lumiverb.preview": _jpeg()}, headers=headers)
    assert r.status_code == 200, r.text
    clip = r.json()["asset_id"]
    assert _row(lib, clip, "proxy").producer == "proxy", "kept as today's id until part 3"
    assert _state(lib, clip, "proxy") == "current"
    again = {"exif": data["exif"], "lineage": data["lineage"]}
    r = client.post(f"/v1/assets/{clip}/ingest", data=again, files={"lumiverb.preview": _jpeg()}, headers=headers)
    assert r.status_code == 200, r.text
    assert _state(lib, clip, "proxy") == "current"
    # Neither spelling of the preview: refused, saying what to send.
    r = client.post("/v1/ingest", data={**data, "rel_path": "none.jpg"}, headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "preview_required", r.text
    r = client.post(f"/v1/assets/{clip}/ingest", data=again, headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "preview_required", r.text


@pytest.mark.parametrize("artifact", ["vision", "ocr", "clip"])
def test_a_lineage_by_name_leaves_nothing_due(env, artifact):
    lib = _library(env, f"NamesDue_{artifact}")
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, {"proxy": _named(lib, "proxy", sha)})
    assert _due(lib, artifact) == [clip]
    kind, body = {
        "vision": ("vision", {"model_id": "m", "description": "a dog on a beach"}),
        "ocr": ("ocr", {"ocr_text": "EXIT", "model_id": "m"}),
        "clip": ("embeddings", {"model_id": "clip", "model_version": "1", "vector": [0.0] * 512}),
    }[artifact]
    r = saved(client, kind, clip, json={**body, "lineage": _named(lib, artifact, sha)}, headers=headers)
    assert r.status_code == 200 and r.json()["updated"] == 1, r.text
    assert _due(lib, artifact) == []
    assert _counts(lib, artifact)["current"] == 1
    assert _row(lib, clip, artifact).producer == {"vision": "vision", "ocr": "ocr", "clip": "clip"}[artifact]


def test_a_model_id_is_never_a_name(env):
    # A visual search vector is made by the CLIP model: a name in its place is another model's, so still due.
    lib = _library(env, "NamesModel")
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha)
    r = saved(client, "embeddings", clip, headers=headers, json={
        "model_id": "lumiverb.image.likeness", "model_version": "1", "vector": [0.0] * 512,
        "lineage": _named(lib, "clip", sha)})
    assert r.status_code == 200, r.text
    assert _due(lib, "clip") == [clip]


def test_an_ai_job_is_never_a_name(env):
    client, headers, *_ = env
    assert client.get("/v1/ai/jobs/clip", headers=headers).status_code != 404
    for job in ("lumiverb.image.likeness", "lumiverb.image.description", "lumiverb.video.transcript"):
        assert client.get(f"/v1/ai/jobs/{job}", headers=headers).status_code == 404, job
        assert client.put(f"/v1/ai/jobs/{job}", json={"model": "x"}, headers=headers).status_code == 404, job


def test_a_file_by_its_name_is_the_file_by_its_kind(env):
    lib = _library(env, "NamesFiles")
    client, headers, *_ = lib
    sha = _sha()
    photo = _ingest_with(lib, "a.jpg", sha)
    for name in ("lumiverb.preview", "lumiverb.preview.thumbnail"):
        r = client.post(f"/v1/assets/{photo}/artifacts/{name}", files={"file": _jpeg()}, headers=headers,
                        data={"lineage": json.dumps(_named(lib, "proxy", sha))})  # a thumbnail's is the preview's
        assert r.status_code == 200, (name, r.text)
        by_name = client.get(f"/v1/assets/{photo}/artifacts/{name}", headers=headers)
        by_kind = client.get(f"/v1/assets/{photo}/artifacts/{FILES[name]}", headers=headers)
        assert by_name.status_code == by_kind.status_code == 200, (name, by_name.text)
        assert by_name.content == by_kind.content and by_name.headers["content-type"] == by_kind.headers["content-type"]
    assert _row(lib, photo, "proxy").producer == "proxy"

    # The batch upload's fields by their names.
    r = client.post(f"/v1/assets/{photo}/artifacts", headers=headers,
                    files={"lumiverb.preview": _jpeg(), "lumiverb.preview.thumbnail": _jpeg()},
                    data={"lineage": json.dumps({"lumiverb.preview": _named(lib, "proxy", sha)})})
    assert r.status_code == 200, r.text
    assert sorted(i["artifact_type"] for i in r.json()["items"]) == ["proxy", "thumbnail"], r.json()

    # A video's analysis proxy: whole, and in chunks.
    video = _ingest_with(lib, "v.mov", _sha(), media_type="video")
    vsha = _row(lib, video, "proxy").source_hash
    r = client.post(f"/v1/assets/{video}/artifacts/lumiverb.video.analysis_proxy", headers=headers,
                    files={"file": ("a.mp4", io.BytesIO(playable()), "video/mp4")},
                    data={"lineage": json.dumps(_named(lib, "analysis_proxy", vsha))})
    assert r.status_code == 200, r.text
    assert _state(lib, video, "analysis_proxy") == "current"
    by_name = client.get(f"/v1/assets/{video}/artifacts/lumiverb.video.analysis_proxy", headers=headers)
    by_kind = client.get(f"/v1/assets/{video}/artifacts/analysis_proxy", headers=headers)
    assert by_name.status_code == by_kind.status_code == 200 and by_name.headers["etag"] == by_kind.headers["etag"]
    body = playable(2.0)
    r = client.post(f"/v1/assets/{video}/uploads", headers=headers, json={
        "artifact": "lumiverb.video.analysis_proxy", "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
        "lineage": _named(lib, "analysis_proxy", vsha)})
    assert r.status_code == 200 and r.json()["artifact"] == "analysis_proxy", r.text
    assert client.delete(f"/v1/uploads/{r.json()['upload_id']}", headers=headers).status_code == 204
    r = client.post(f"/v1/assets/{video}/uploads", headers=headers, json={
        "artifact": "lumiverb.video.scenes.picture", "size": 1, "sha256": "0" * 64, "lineage": {}})
    assert r.status_code == 422, r.text  # a scene's picture isn't sent in chunks, by either spelling


def _producer(env, artifact: str) -> dict:
    client, headers, *_ = env
    r = client.get("/v1/producers", params={"counts": "false"}, headers=headers)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p for p in r.json()["producers"]}[artifact]


def test_the_producers_routes_take_a_name(env):
    lib = _library(env, "NamesProducers")
    client, headers, library_id, *_ = lib
    clip = _ingest_with(lib, "a.jpg", _sha())

    r = client.put("/v1/producers/lumiverb.video.transcript/settings", headers=headers,
                   json={"settings": {"vad_min_silence_ms": 800}, "redo": True})
    assert r.status_code == 200 and r.json()["artifact"] == "transcript", r.text
    assert _producer(env, "transcript")["settings"]["vad_min_silence_ms"] == 800
    assert client.put("/v1/producers/transcript/settings", headers=headers,
                      json={"settings": {"vad_min_silence_ms": None}, "redo": True}).status_code == 200

    for scope, field in (("work", "paused"), ("redo", "redo_stopped")):
        r = client.post("/v1/producers/lumiverb.image.description/pause", json={"scope": scope}, headers=headers)
        assert r.status_code == 204, r.text
        assert _producer(env, "vision")[field] is True, scope
        r = client.post("/v1/producers/lumiverb.image.description/resume", json={"scope": scope}, headers=headers)
        assert r.status_code == 204 and _producer(env, "vision")[field] is False, scope

    r = client.post("/v1/producers/failures", headers=headers, json={"items": [
        {"asset_id": clip, "artifact": "lumiverb.image.text", "error": "unreadable"}]})
    assert r.status_code == 200 and r.json()["recorded"] == 1, r.text
    by_name = client.get("/v1/producers/failures", params={"artifact": "lumiverb.image.text", "library_id": library_id},
                         headers=headers)
    by_id = client.get("/v1/producers/failures", params={"artifact": "ocr", "library_id": library_id}, headers=headers)
    assert by_name.status_code == by_id.status_code == 200 and by_name.json() == by_id.json()
    assert [(i["asset_id"], i["artifact"]) for i in by_name.json()["items"]] == [(clip, "ocr")]
    r = client.post("/v1/producers/failures/retry", headers=headers,
                    json={"all": True, "artifact": "lumiverb.image.text", "library_id": library_id})
    assert r.status_code == 200 and r.json()["retried"] == 1, r.text

    r = client.post("/v1/producers/run", headers=headers,
                    json={"producer": "lumiverb.image.description", "library_ids": [library_id], "scope": "new"})
    assert r.status_code == 200 and [p["artifact"] for p in r.json()["producers"]] == ["vision"], r.text
    by_name = client.post("/v1/producers/lumiverb.image.faces/below/redo", headers=headers,
                          json={"all": True, "library_id": library_id})
    by_id = client.post("/v1/producers/faces/below/redo", headers=headers, json={"all": True, "library_id": library_id})
    assert by_name.status_code == by_id.status_code == 200 and by_name.json() == by_id.json(), by_name.text

    # A name nothing makes is as unknown as an id nothing makes.
    for target in ("lumiverb.image.nothing", "lumiverb.people"):
        assert client.put(f"/v1/producers/{target}/settings", json={"settings": {}}, headers=headers).status_code == 404
        r = client.post("/v1/producers/failures", headers=headers, json={"items": [
            {"asset_id": clip, "artifact": target, "error": "x"}]})
        assert r.status_code == 422, r.text
