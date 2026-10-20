# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The server stores and sends the names (ADR-024 phase 1 part 3), and still
takes an older client's words until part 5 (src/producers/names.py RENAMED):
every route sent an artifact or a lineage's producer by its old id gives the
same answer as by its name, and keeps and answers the name; from part 4 a
file too, by its old kind or its name. What's never renamed (AI jobs, model
ids, protocol 1's inputs) isn't taken by a name.

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

from src.producers import registry
from src.producers.names import ARTIFACTS, FILES, PRODUCERS, makers
from tests.derived import saved
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _ingest_with, _row, _sha, _state, _want
from tests.test_reconciler import _counts, _due, _library
from tests.videos import playable

pytestmark = pytest.mark.slow

_OLD_PRODUCER = {name: producer for (_, producer), name in PRODUCERS.items()}


def _old(env, name: str, sha: str | None) -> dict:
    """Lineage as a client from before part 3 sends it: its producer by its old id."""
    return {**_want(env, name, sha), "producer": _OLD_PRODUCER[name]}


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


@pytest.mark.parametrize("key, field", [("proxy", "proxy"), ("lumiverb.preview", "lumiverb.preview")])
def test_a_clip_ingested_by_old_words_is_kept_by_names(env, key, field):
    lib = _library(env, f"NamesIngest_{field}")
    client, headers, library_id, *_ = lib
    sha = _sha()
    lineage = _old(lib, "lumiverb.preview", sha) if key == "proxy" else _want(lib, "lumiverb.preview", sha)
    data = {"library_id": library_id, "rel_path": "named.jpg", "file_size": "1000", "media_type": "image",
            "width": "64", "height": "36", "exif": json.dumps({"content_hash": sha}),
            "lineage": json.dumps({key: lineage})}
    r = client.post("/v1/ingest", data=data, files={field: _jpeg()}, headers=headers)
    assert r.status_code == 200, r.text
    clip = r.json()["asset_id"]
    assert _row(lib, clip, "lumiverb.preview").producer == "lumiverb.preview", "kept by its name"
    assert _state(lib, clip, "lumiverb.preview") == "current"
    # Neither spelling of the preview: refused, saying what to send.
    r = client.post("/v1/ingest", data={**data, "rel_path": "none.jpg"}, headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "preview_required", r.text


@pytest.mark.parametrize("name", ["lumiverb.image.description", "lumiverb.image.text", "lumiverb.image.likeness"])
def test_a_lineage_by_its_old_producer_leaves_nothing_due(env, name):
    lib = _library(env, f"NamesDue_{name}")
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha)
    assert _due(lib, name) == [clip]
    kind, body = {
        "lumiverb.image.description": ("vision", {"model_id": "m", "description": "a dog on a beach"}),
        "lumiverb.image.text": ("ocr", {"ocr_text": "EXIT", "model_id": "m"}),
        "lumiverb.image.likeness": ("embeddings", {"model_id": "clip", "model_version": "1", "vector": [0.0] * 512}),
    }[name]
    r = saved(client, kind, clip, json={**body, "lineage": _old(lib, name, sha)}, headers=headers)
    assert r.status_code == 200 and r.json()["updated"] == 1, r.text
    assert _due(lib, name) == []
    assert _counts(lib, name)["current"] == 1
    assert _row(lib, clip, name).producer == name


@pytest.mark.parametrize("artifact, producer", sorted(PRODUCERS))
def test_each_old_producer_is_kept_by_its_name_and_current(env, artifact, producer):
    # Every machine write goes through require_lineage, then lineage.record: an old id kept as anything but the
    # name would be an unknown producer's, stale, and made again.
    from sqlmodel import Session

    from src.server.api.routers.producers import require_lineage
    from src.server.database import get_engine_for_url
    from src.server.repository import lineage

    name = ARTIFACTS[artifact]
    lib = _library(env, f"NamesEach_{artifact}")
    sha = _sha()
    media = "image" if "image" in registry()[name].media else "video"
    clip = _ingest_with(lib, "a.jpg" if media == "image" else "a.mov", sha, media_type=media)
    old = {**_want(lib, name, sha), "producer": producer}
    made = require_lineage(old, name)
    assert made["producer"] == name
    with Session(get_engine_for_url(env[-1])) as session:
        lineage.record(session, clip, name, made)
    assert _row(lib, clip, name).producer == name
    assert _state(lib, clip, name) == "current"


def test_a_model_id_is_never_a_name(env):
    # A visual search vector is made by the CLIP model: a name in its place is another model's, so still due.
    lib = _library(env, "NamesModel")
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha)
    r = saved(client, "embeddings", clip, headers=headers, json={
        "model_id": "lumiverb.image.likeness", "model_version": "1", "vector": [0.0] * 512,
        "lineage": _want(lib, "lumiverb.image.likeness", sha)})
    assert r.status_code == 200, r.text
    assert _due(lib, "lumiverb.image.likeness") == [clip]


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
                        data={"lineage": json.dumps(_want(lib, "lumiverb.preview", sha))})  # a thumbnail: the preview's
        assert r.status_code == 200, (name, r.text)
        by_name = client.get(f"/v1/assets/{photo}/artifacts/{name}", headers=headers)
        by_kind = client.get(f"/v1/assets/{photo}/artifacts/{FILES[name]}", headers=headers)
        assert by_name.status_code == by_kind.status_code == 200, (name, by_name.text)
        assert by_name.content == by_kind.content and by_name.headers["content-type"] == by_kind.headers["content-type"]
    assert _row(lib, photo, "lumiverb.preview").producer == "lumiverb.preview"

    # The batch upload's fields by their names, its lineage by an old word; its items say the names.
    r = client.post(f"/v1/assets/{photo}/artifacts", headers=headers,
                    files={"lumiverb.preview": _jpeg(), "lumiverb.preview.thumbnail": _jpeg()},
                    data={"lineage": json.dumps({"proxy": _old(lib, "lumiverb.preview", sha)})})
    assert r.status_code == 200, r.text
    assert sorted(i["artifact_type"] for i in r.json()["items"]) == ["lumiverb.preview", "lumiverb.preview.thumbnail"]
    # And by their old kinds (until part 5), answered by name.
    r = client.post(f"/v1/assets/{photo}/artifacts", headers=headers, files={"proxy": _jpeg()},
                    data={"lineage": json.dumps({"lumiverb.preview": _want(lib, "lumiverb.preview", sha)})})
    assert r.status_code == 200 and [i["artifact_type"] for i in r.json()["items"]] == ["lumiverb.preview"], r.text

    # A video's analysis proxy: whole, and in chunks, by its name or its old kind; answered by name.
    video = _ingest_with(lib, "v.mov", _sha(), media_type="video")
    vsha = _row(lib, video, "lumiverb.preview").source_hash
    r = client.post(f"/v1/assets/{video}/artifacts/lumiverb.video.analysis_proxy", headers=headers,
                    files={"file": ("a.mp4", io.BytesIO(playable()), "video/mp4")},
                    data={"lineage": json.dumps(_old(lib, "lumiverb.video.analysis_proxy", vsha))})
    assert r.status_code == 200, r.text
    assert _state(lib, video, "lumiverb.video.analysis_proxy") == "current"
    by_name = client.get(f"/v1/assets/{video}/artifacts/lumiverb.video.analysis_proxy", headers=headers)
    by_kind = client.get(f"/v1/assets/{video}/artifacts/analysis_proxy", headers=headers)
    assert by_name.status_code == by_kind.status_code == 200 and by_name.headers["etag"] == by_kind.headers["etag"]
    body = playable(2.0)
    r = client.post(f"/v1/assets/{video}/uploads", headers=headers, json={
        "artifact": "lumiverb.video.analysis_proxy", "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
        "lineage": _want(lib, "lumiverb.video.analysis_proxy", vsha)})
    assert r.status_code == 200 and r.json()["artifact"] == "lumiverb.video.analysis_proxy", r.text
    assert client.delete(f"/v1/uploads/{r.json()['upload_id']}", headers=headers).status_code == 204
    r = client.post(f"/v1/assets/{video}/uploads", headers=headers, json={
        "artifact": "analysis_proxy", "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
        "lineage": _want(lib, "lumiverb.video.analysis_proxy", vsha)})
    assert r.status_code == 200 and r.json()["artifact"] == "lumiverb.video.analysis_proxy", r.text
    assert client.delete(f"/v1/uploads/{r.json()['upload_id']}", headers=headers).status_code == 204
    r = client.post(f"/v1/assets/{video}/uploads", headers=headers, json={
        "artifact": "lumiverb.video.scenes.picture", "size": 1, "sha256": "0" * 64, "lineage": {}})
    assert r.status_code == 422, r.text  # a scene's picture isn't sent in chunks, by either spelling


def _producers(env) -> dict:
    client, headers, *_ = env
    r = client.get("/v1/producers", params={"counts": "false"}, headers=headers)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p for p in r.json()["producers"]}


def test_get_producers_answers_names_and_what_each_reads(env):
    rows = _producers(env)
    assert set(rows) == set(registry())
    for name, row in rows.items():
        assert row["producer"] == name, "a producer is named by what it makes"
        assert row["reads"] == list(registry()[name].reads)
        assert all(n in rows for n in row["needs"]) and (not row["part_of"] or row["part_of"] in rows)
    assert rows["lumiverb.location.guess"]["reads_others"] == ["lumiverb.capture_time", "lumiverb.location.gps",
                                                               "lumiverb.location.person"]


def test_the_producers_routes_take_an_old_word_and_answer_the_name(env):
    lib = _library(env, "NamesProducers")
    client, headers, library_id, *_ = lib
    clip = _ingest_with(lib, "a.jpg", _sha())

    r = client.put("/v1/producers/transcript/settings", headers=headers,
                   json={"settings": {"vad_min_silence_ms": 800}, "redo": True})
    assert r.status_code == 200 and r.json()["artifact"] == "lumiverb.video.transcript", r.text
    assert _producers(env)["lumiverb.video.transcript"]["settings"]["vad_min_silence_ms"] == 800
    assert client.put("/v1/producers/lumiverb.video.transcript/settings", headers=headers,
                      json={"settings": {"vad_min_silence_ms": None}, "redo": True}).status_code == 200

    for scope, field in (("work", "paused"), ("redo", "redo_stopped")):
        r = client.post("/v1/producers/vision/pause", json={"scope": scope}, headers=headers)
        assert r.status_code == 204, r.text
        assert _producers(env)["lumiverb.image.description"][field] is True, scope
        r = client.post("/v1/producers/vision/resume", json={"scope": scope}, headers=headers)
        assert r.status_code == 204 and _producers(env)["lumiverb.image.description"][field] is False, scope

    r = client.post("/v1/producers/failures", headers=headers, json={"items": [
        {"asset_id": clip, "artifact": "ocr", "error": "unreadable"}]})
    assert r.status_code == 200 and r.json()["recorded"] == 1, r.text
    by_old = client.get("/v1/producers/failures", params={"artifact": "ocr", "library_id": library_id},
                        headers=headers)
    by_name = client.get("/v1/producers/failures", params={"artifact": "lumiverb.image.text",
                                                           "library_id": library_id}, headers=headers)
    assert by_old.status_code == by_name.status_code == 200 and by_old.json() == by_name.json()
    assert [(i["asset_id"], i["artifact"]) for i in by_name.json()["items"]] == [(clip, "lumiverb.image.text")]
    r = client.post("/v1/producers/failures/retry", headers=headers,
                    json={"all": True, "artifact": "ocr", "library_id": library_id})
    assert r.status_code == 200 and r.json()["retried"] == 1, r.text

    r = client.post("/v1/producers/run", headers=headers,
                    json={"producer": "vision", "library_ids": [library_id], "scope": "new"})
    assert r.status_code == 200, r.text
    assert [p["artifact"] for p in r.json()["producers"]] == ["lumiverb.image.description"]
    by_old = client.post("/v1/producers/faces/below/redo", headers=headers, json={"all": True, "library_id": library_id})
    by_name = client.post("/v1/producers/lumiverb.image.faces/below/redo", headers=headers,
                          json={"all": True, "library_id": library_id})
    assert by_old.status_code == by_name.status_code == 200 and by_old.json() == by_name.json(), by_old.text

    # What nothing makes is unknown, by either spelling.
    for target in ("lumiverb.image.nothing", "lumiverb.people", "nothing"):
        assert client.put(f"/v1/producers/{target}/settings", json={"settings": {}}, headers=headers).status_code == 404
        r = client.post("/v1/producers/failures", headers=headers, json={"items": [
            {"asset_id": clip, "artifact": target, "error": "x"}]})
        assert r.status_code == 422, r.text


def test_the_queue_answers_names(env):
    client, headers, *_ = env
    r = client.get("/v1/producers/queue", headers=headers)
    assert r.status_code == 200, r.text
    targets = [sw["target"] for sw in r.json()["switches"]]
    assert "upkeep" in targets and "lumiverb.image.description" in targets
    assert not set(targets) & set(ARTIFACTS), "no old word"
