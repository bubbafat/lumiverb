# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The server takes and answers the names (ADR-024 phase 1), and since part
5 refuses an old spelling wherever a client sends one, saying the name: 404
renamed for a URL (details: what was sent and its name), 422 for a body or
a form (renamed, or the field's error naming it), a lineage by its old
producer 422 lineage_wrong_producer, naming the producer. Nothing is kept
or changed. What's never renamed (AI jobs, model ids, protocol 1's inputs)
isn't taken by a name.

Against the real API and database (tests/test_analysis_proxy_api.py's
account). A device's gate lets an old word on to the route, which refuses
it: tests/test_device_keys.py. The CLIs refuse one before sending:
test_cli_pause.py, test_cli_producers.py, test_server_pausing.py.
"""

from __future__ import annotations

import hashlib
import io
import json

import pytest
from PIL import Image
from sqlalchemy import create_engine, inspect, text

from src.producers import registry
from src.producers.names import FILES, WAS, makers
from tests.derived import saved
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _row, _sha, _state, _want
from tests.test_reconciler import _counts, _due, _library
from tests.videos import playable

pytestmark = pytest.mark.slow

# Each name's old words, as the history keeps them (src/producers/names.py WAS).
OLD_ARTIFACTS = {was["artifact"]: name for name, was in WAS.items() if "artifact" in was}
OLD_PRODUCERS = sorted((name, was["producer"]) for name, was in WAS.items() if "producer" in was)


def _jpeg() -> tuple:
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color=(40, 50, 60)).save(buf, format="JPEG")
    return ("p.jpg", io.BytesIO(buf.getvalue()), "image/jpeg")


def _renamed(r, status: int, sent: str, name: str) -> None:
    """Refused, saying what was sent and its name."""
    assert r.status_code == status, r.text
    error = r.json()["error"]
    assert error["code"] == "renamed" and error["details"] == {"sent": sent, "name": name}, r.text
    assert name in error["message"], r.text


def _said(r, sent: str, name: str) -> None:
    """A field's refusal (422): its message says the name."""
    assert r.status_code == 422, r.text
    assert f"{sent} is called {name} now" in r.text, r.text


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


def test_a_clip_is_ingested_by_names_and_refused_by_old_words(env):
    lib = _library(env, "NamesIngest")
    client, headers, library_id, *_ = lib
    sha = _sha()
    data = {"library_id": library_id, "rel_path": "named.jpg", "file_size": "1000", "media_type": "image",
            "width": "64", "height": "36", "exif": json.dumps({"content_hash": sha}),
            "lineage": json.dumps({"lumiverb.preview": _want(lib, "lumiverb.preview", sha)})}
    # The preview by its old kind, its lineage by its old id: refused, nothing kept.
    _renamed(client.post("/v1/ingest", data={**data, "rel_path": "old.jpg"}, files={"proxy": _jpeg()},
                         headers=headers), 422, "proxy", "lumiverb.preview")
    _renamed(client.post("/v1/ingest", data={**data, "rel_path": "old.jpg", "lineage": json.dumps(
        {"proxy": _want(lib, "lumiverb.preview", sha)})}, files={"lumiverb.preview": _jpeg()}, headers=headers),
        422, "proxy", "lumiverb.preview")
    with _db(lib) as s:
        assert s.execute(text("SELECT count(*) FROM assets WHERE rel_path = 'old.jpg'")).scalar() == 0
    # By the names: kept by its name, current.
    r = client.post("/v1/ingest", data=data, files={"lumiverb.preview": _jpeg()}, headers=headers)
    assert r.status_code == 200, r.text
    clip = r.json()["asset_id"]
    assert _row(lib, clip, "lumiverb.preview").producer == "lumiverb.preview", "kept by its name"
    assert _state(lib, clip, "lumiverb.preview") == "current"
    # No preview at all: refused, saying what to send.
    r = client.post("/v1/ingest", data={**data, "rel_path": "none.jpg"}, headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "preview_required", r.text
    assert "lumiverb.preview" in r.json()["error"]["message"]


@pytest.mark.parametrize("name", ["lumiverb.image.description", "lumiverb.image.text", "lumiverb.image.likeness"])
def test_a_lineage_by_its_old_producer_is_refused_and_still_due(env, name):
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
    old = {**_want(lib, name, sha), "producer": WAS[name]["producer"]}
    r = saved(client, kind, clip, json={**body, "lineage": old}, headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "lineage_wrong_producer", r.text
    assert r.json()["error"]["details"]["expected"] == name
    assert _due(lib, name) == [clip]
    assert _counts(lib, name)["current"] == 0
    r = saved(client, kind, clip, json={**body, "lineage": _want(lib, name, sha)}, headers=headers)
    assert r.status_code == 200 and r.json()["updated"] == 1, r.text
    assert _due(lib, name) == [] and _row(lib, clip, name).producer == name


@pytest.mark.parametrize("name, producer", OLD_PRODUCERS)
def test_each_old_producer_is_refused_naming_the_producer(env, name, producer):
    # Every machine write goes through require_lineage: an old id is another producer's, refused before anything
    # is saved, saying which producer makes it.
    from src.server.api.errors import InvalidChoiceError
    from src.server.api.routers.producers import require_lineage

    lib = _library(env, f"NamesEach_{name}")
    old = {**_want(lib, name, _sha()), "producer": producer}
    with pytest.raises(InvalidChoiceError) as refused:
        require_lineage(old, name)
    assert refused.value.code == "lineage_wrong_producer"
    assert refused.value.details == {"artifact": name, "sent": producer, "expected": name}
    assert name in refused.value.message


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


def test_a_file_is_sent_and_asked_for_by_its_name_and_refused_by_its_old_kind(env):
    lib = _library(env, "NamesFiles")
    client, headers, *_ = lib
    sha = _sha()
    photo = _ingest_with(lib, "a.jpg", sha)
    for name in ("lumiverb.preview", "lumiverb.preview.thumbnail"):
        old = WAS[name]["file"]
        lineage = {"lineage": json.dumps(_want(lib, "lumiverb.preview", sha))}  # a thumbnail: the preview's
        _renamed(client.post(f"/v1/assets/{photo}/artifacts/{old}", files={"file": _jpeg()}, headers=headers,
                             data=lineage), 404, old, name)
        r = client.post(f"/v1/assets/{photo}/artifacts/{name}", files={"file": _jpeg()}, headers=headers, data=lineage)
        assert r.status_code == 200, (name, r.text)
        assert client.get(f"/v1/assets/{photo}/artifacts/{name}", headers=headers).status_code == 200, name
        _renamed(client.get(f"/v1/assets/{photo}/artifacts/{old}", headers=headers), 404, old, name)
    # Every file's old kind, asked for: refused, naming it.
    for name in FILES:
        _renamed(client.get(f"/v1/assets/{photo}/artifacts/{WAS[name]['file']}", headers=headers), 404,
                 WAS[name]["file"], name)
    assert _row(lib, photo, "lumiverb.preview").producer == "lumiverb.preview"

    # The batch upload: its fields and its lineage by their names; by an old kind or id, refused.
    r = client.post(f"/v1/assets/{photo}/artifacts", headers=headers,
                    files={"lumiverb.preview": _jpeg(), "lumiverb.preview.thumbnail": _jpeg()},
                    data={"lineage": json.dumps({"lumiverb.preview": _want(lib, "lumiverb.preview", sha)})})
    assert r.status_code == 200, r.text
    assert sorted(i["artifact_type"] for i in r.json()["items"]) == ["lumiverb.preview", "lumiverb.preview.thumbnail"]
    for field, name in (("proxy", "lumiverb.preview"), ("thumbnail", "lumiverb.preview.thumbnail"),
                        ("video_preview", "lumiverb.video.preview")):
        _renamed(client.post(f"/v1/assets/{photo}/artifacts", headers=headers, files={field: _jpeg()},
                             data={"lineage": json.dumps({"lumiverb.preview": _want(lib, "lumiverb.preview", sha)})}),
                 422, field, name)
    _renamed(client.post(f"/v1/assets/{photo}/artifacts", headers=headers, files={"lumiverb.preview": _jpeg()},
                         data={"lineage": json.dumps({"proxy": _want(lib, "lumiverb.preview", sha)})}),
             422, "proxy", "lumiverb.preview")

    # A video's analysis proxy: whole, and in chunks, by its name; by its old kind, refused.
    video = _ingest_with(lib, "v.mov", _sha(), media_type="video")
    vsha = _row(lib, video, "lumiverb.preview").source_hash
    lineage = {"lineage": json.dumps(_want(lib, "lumiverb.video.analysis_proxy", vsha))}
    _renamed(client.post(f"/v1/assets/{video}/artifacts/analysis_proxy", headers=headers, data=lineage,
                         files={"file": ("a.mp4", io.BytesIO(playable()), "video/mp4")}),
             404, "analysis_proxy", "lumiverb.video.analysis_proxy")
    r = client.post(f"/v1/assets/{video}/artifacts/lumiverb.video.analysis_proxy", headers=headers, data=lineage,
                    files={"file": ("a.mp4", io.BytesIO(playable()), "video/mp4")})
    assert r.status_code == 200, r.text
    assert _state(lib, video, "lumiverb.video.analysis_proxy") == "current"
    body = playable(2.0)
    start = {"size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
             "lineage": _want(lib, "lumiverb.video.analysis_proxy", vsha)}
    for old, name in (("analysis_proxy", "lumiverb.video.analysis_proxy"), ("video_preview", "lumiverb.video.preview")):
        _said(client.post(f"/v1/assets/{video}/uploads", headers=headers, json={**start, "artifact": old}), old, name)
    r = client.post(f"/v1/assets/{video}/uploads", headers=headers,
                    json={**start, "artifact": "lumiverb.video.analysis_proxy"})
    assert r.status_code == 200 and r.json()["artifact"] == "lumiverb.video.analysis_proxy", r.text
    assert client.delete(f"/v1/uploads/{r.json()['upload_id']}", headers=headers).status_code == 204
    r = client.post(f"/v1/assets/{video}/uploads", headers=headers, json={
        "artifact": "lumiverb.video.scenes.picture", "size": 1, "sha256": "0" * 64, "lineage": {}})
    assert r.status_code == 422, r.text  # a scene's picture isn't sent in chunks


def _producers(env) -> dict:
    client, headers, *_ = env
    r = client.get("/v1/producers", params={"counts": "false"}, headers=headers)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p for p in r.json()["producers"]}


def test_get_producers_answers_names_and_what_each_reads(env):
    rows = _producers(env)
    assert set(rows) == set(registry())
    for name, row in rows.items():
        assert "producer" not in row, "a producer is named by what it makes: its artifact (part 5)"
        assert row["reads"] == list(registry()[name].reads)
        assert all(n in rows for n in row["needs"]) and (not row["part_of"] or row["part_of"] in rows)
    assert rows["lumiverb.location.guess"]["reads_others"] == ["lumiverb.capture_time", "lumiverb.location.gps",
                                                               "lumiverb.location.person"]


def test_the_producers_routes_take_a_name_and_refuse_an_old_word_naming_it(env):
    lib = _library(env, "NamesProducers")
    client, headers, library_id, *_ = lib
    clip = _ingest_with(lib, "a.jpg", _sha())

    # A URL naming an old word: 404 renamed, nothing changed.
    _renamed(client.put("/v1/producers/transcript/settings", headers=headers,
                        json={"settings": {"vad_min_silence_ms": 800}, "redo": True}),
             404, "transcript", "lumiverb.video.transcript")
    assert _producers(env)["lumiverb.video.transcript"]["settings"]["vad_min_silence_ms"] != 800
    r = client.put("/v1/producers/lumiverb.video.transcript/settings", headers=headers,
                   json={"settings": {"vad_min_silence_ms": 800}, "redo": True})
    assert r.status_code == 200 and r.json()["artifact"] == "lumiverb.video.transcript", r.text
    assert client.put("/v1/producers/lumiverb.video.transcript/settings", headers=headers,
                      json={"settings": {"vad_min_silence_ms": None}, "redo": True}).status_code == 200

    for scope, field in (("work", "paused"), ("redo", "redo_stopped")):
        for action in ("pause", "resume"):
            _renamed(client.post(f"/v1/producers/vision/{action}", json={"scope": scope}, headers=headers),
                     404, "vision", "lumiverb.image.description")
            assert _producers(env)["lumiverb.image.description"][field] is False, scope
        r = client.post("/v1/producers/lumiverb.image.description/pause", json={"scope": scope}, headers=headers)
        assert r.status_code == 204 and _producers(env)["lumiverb.image.description"][field] is True, r.text
        r = client.post("/v1/producers/lumiverb.image.description/resume", json={"scope": scope}, headers=headers)
        assert r.status_code == 204 and _producers(env)["lumiverb.image.description"][field] is False, r.text
    # A plug-in's work before part 3, plugin_<id>: its id now.
    _renamed(client.post("/v1/producers/plugin_geonames/pause", json={"scope": "work"}, headers=headers),
             404, "plugin_geonames", "geonames")

    # A body naming an old word: 422 naming it, nothing recorded.
    _said(client.post("/v1/producers/failures", headers=headers, json={"items": [
        {"asset_id": clip, "artifact": "ocr", "error": "unreadable"}]}), "ocr", "lumiverb.image.text")
    _renamed(client.get("/v1/producers/failures", params={"artifact": "ocr", "library_id": library_id},
                        headers=headers), 404, "ocr", "lumiverb.image.text")
    r = client.get("/v1/producers/failures", params={"artifact": "lumiverb.image.text", "library_id": library_id},
                   headers=headers)
    assert r.status_code == 200 and r.json()["items"] == [], r.text
    r = client.post("/v1/producers/failures", headers=headers, json={"items": [
        {"asset_id": clip, "artifact": "lumiverb.image.text", "error": "unreadable"}]})
    assert r.status_code == 200 and r.json()["recorded"] == 1, r.text
    _said(client.post("/v1/producers/failures/retry", headers=headers,
                      json={"all": True, "artifact": "ocr", "library_id": library_id}), "ocr", "lumiverb.image.text")
    r = client.post("/v1/producers/failures/retry", headers=headers,
                    json={"all": True, "artifact": "lumiverb.image.text", "library_id": library_id})
    assert r.status_code == 200 and r.json()["retried"] == 1, r.text

    _said(client.post("/v1/producers/run", headers=headers,
                      json={"producer": "vision", "library_ids": [library_id], "scope": "new"}),
          "vision", "lumiverb.image.description")
    r = client.post("/v1/producers/run", headers=headers,
                    json={"producer": "lumiverb.image.description", "library_ids": [library_id], "scope": "new"})
    assert r.status_code == 200, r.text
    assert [p["artifact"] for p in r.json()["producers"]] == ["lumiverb.image.description"]
    _renamed(client.post("/v1/producers/faces/below/redo", headers=headers, json={"all": True, "library_id": library_id}),
             404, "faces", "lumiverb.image.faces")
    r = client.post("/v1/producers/lumiverb.image.faces/below/redo", headers=headers,
                    json={"all": True, "library_id": library_id})
    assert r.status_code == 200, r.text

    # What nothing makes, and never made, is unknown.
    for target in ("lumiverb.image.nothing", "lumiverb.people", "nothing"):
        r = client.put(f"/v1/producers/{target}/settings", json={"settings": {}}, headers=headers)
        assert r.status_code == 404 and r.json()["error"]["code"] != "renamed", r.text
        r = client.post("/v1/producers/failures", headers=headers, json={"items": [
            {"asset_id": clip, "artifact": target, "error": "x"}]})
        assert r.status_code == 422 and "no producer makes" in r.text, r.text


@pytest.mark.parametrize("old", sorted(OLD_ARTIFACTS))
def test_every_old_artifact_id_in_a_url_is_refused_naming_it(env, old):
    client, headers, *_ = env
    _renamed(client.get("/v1/producers/failures", params={"artifact": old}, headers=headers), 404, old,
             OLD_ARTIFACTS[old])


def test_the_queue_answers_names(env):
    client, headers, *_ = env
    r = client.get("/v1/producers/queue", headers=headers)
    assert r.status_code == 200, r.text
    targets = [sw["target"] for sw in r.json()["switches"]]
    assert "upkeep" in targets and "lumiverb.image.description" in targets
    assert not set(targets) & set(OLD_ARTIFACTS), "no old word"
