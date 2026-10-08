# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Lineage on every write, and what's current (ADR-016 phase 3, piece 1).

A write records how its artifact was made: producer, version, settings
hash, source SHA-256. GET /v1/producers says, per producer, how many clips'
artifacts are current, stale, missing or failing: stale when any of the four
differs from what's registered now. A write that doesn't say is an unknown
producer's: never current. A person's transcript is a person's: current.
"""

from __future__ import annotations

import io
import json
import os
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import Session

from src.shared import producers as P
from tests.test_analysis_proxy_api import _ingest, env  # noqa: F401 — the shared server fixture


def _sha() -> str:
    return os.urandom(32).hex()


@contextmanager
def _db(env):
    engine = create_engine(env[-1])
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def _producers(env, **params) -> dict:
    client, headers, *_ = env
    r = client.get("/v1/producers", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p for p in r.json()["producers"]}


def _want(env, artifact: str, sha: str | None) -> dict:
    """Lineage as a current worker would send it."""
    p = _producers(env, counts="false")[artifact]
    return {"producer": p["producer"], "version": p["version"], "settings_hash": p["settings_hash"],
            "source_sha256": sha}


def _row(env, asset_id: str, artifact: str):
    with _db(env) as s:
        return s.execute(text("SELECT producer, producer_version, settings_hash, source_sha256, outcome"
                              " FROM artifact_lineage WHERE asset_id = :a AND artifact = :k"),
                         {"a": asset_id, "k": artifact}).first()


def _state(env, asset_id: str, artifact: str) -> str:
    """current, stale or missing for one clip, by the same rules as the counts."""
    client, headers, library_id, *_ = env
    from src.server.repository import lineage as L

    with _db(env) as s:
        want = L.desired(s, artifact)
        row = s.execute(text(
            "SELECT CASE WHEN l.asset_id IS NULL OR l.producer = '' THEN 'missing'"
            f" WHEN {L._stale_sql()} THEN 'stale' ELSE 'current' END"
            " FROM assets a LEFT JOIN artifact_lineage l ON l.asset_id = a.asset_id AND l.artifact = :artifact"
            " WHERE a.asset_id = :a"
        ), {"a": asset_id, "artifact": artifact, "person": P.PERSON, "producer": want["producer"],
            "version": want["version"], "hash": want["settings_hash"]}).scalar()
    return row


def _ingest_with(env, rel_path: str, sha: str, lineage: dict | None, media_type: str = "image") -> str:
    from PIL import Image

    client, headers, library_id, *_ = env
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color=(10, 20, 30)).save(buf, format="JPEG")
    buf.seek(0)
    data = {"library_id": library_id, "rel_path": rel_path, "file_size": "1000", "media_type": media_type,
            "width": "64", "height": "36", "exif": json.dumps({"sha256": sha})}
    if lineage is not None:
        data["lineage"] = json.dumps(lineage)
    r = client.post("/v1/ingest", data=data, files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


# ---------------------------------------------------------------------------
# The producers and their settings
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_every_producer_with_its_settings_and_counts(env):
    producers = _producers(env)
    assert set(producers) == set(P.ARTIFACTS)
    t = producers["transcript"]
    assert (t["producer"], t["version"], t["settings"]["model"]) == ("whisper", "1", "small")
    assert t["settings_hash"] == P.settings_hash(t["settings"])
    assert set(t["counts"]) == {"applicable", "current", "stale", "missing", "failing"}
    assert producers["clip"]["uniform"] is True and producers["transcript"]["uniform"] is False
    assert _producers(env, counts="false")["vision"]["counts"] is None


@pytest.mark.slow
def test_the_worker_records_its_vision_model_once(env):
    client, headers, *_ = env
    r = client.post("/v1/producers/vision-model", json={"model": "qwen3-vl:8b"}, headers=headers)
    assert r.status_code == 200, r.text
    vision = {p["artifact"]: p for p in r.json()["producers"]}
    assert vision["vision"]["settings"]["model"] == "qwen3-vl:8b"
    assert vision["ocr"]["settings"]["model"] == "qwen3-vl:8b"
    assert vision["scene_vision"]["settings"]["model"] == "qwen3-vl:8b"
    # The same again is fine; another is a setting change, not this.
    assert client.post("/v1/producers/vision-model", json={"model": "qwen3-vl:8b"}, headers=headers).status_code == 200
    r = client.post("/v1/producers/vision-model", json={"model": "llava:13b"}, headers=headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "vision_model_set"


# ---------------------------------------------------------------------------
# Every write records lineage
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_an_ingest_that_says_how_its_proxy_was_made_is_current(env):
    sha = _sha()
    clip = _ingest_with(env, "lin/current.jpg", sha, {"proxy": _want(env, "proxy", sha)})
    assert _row(env, clip, "proxy")[0] == "proxy" and _state(env, clip, "proxy") == "current"


@pytest.mark.slow
def test_a_write_that_doesnt_say_is_an_unknown_producers_and_stale(env):
    clip = _ingest(env, "lin/unknown.jpg", sha=_sha())
    assert _row(env, clip, "proxy")[0] == P.UNKNOWN
    assert _state(env, clip, "proxy") == "stale"


@pytest.mark.slow
def test_another_kinds_producer_cant_make_this_one(env):
    sha = _sha()
    clip = _ingest_with(env, "lin/wrongkind.jpg", sha, {"proxy": _want(env, "faces", sha)})
    assert _row(env, clip, "proxy")[0] == P.UNKNOWN


@pytest.mark.slow
def test_a_changed_file_makes_what_was_made_from_it_stale(env):
    old, new = _sha(), _sha()
    clip = _ingest_with(env, "lin/changed.jpg", old, {"proxy": _want(env, "proxy", old)})
    client, headers, *_ = env
    r = client.post("/v1/assets/batch-vision", json={
        "items": [{"asset_id": clip, "model_id": "m", "description": "d", "tags": []}],
        "lineage": _want(env, "vision", old)}, headers=headers)
    assert r.status_code == 200, r.text
    assert _state(env, clip, "vision") == "current"
    with _db(env) as s:  # the file's content changed on disk (a scan saw a new hash)
        s.execute(text("UPDATE assets SET sha256 = :n WHERE asset_id = :a"), {"n": new, "a": clip})
        s.commit()
    assert _state(env, clip, "vision") == "stale" and _state(env, clip, "proxy") == "stale"


@pytest.mark.slow
def test_changing_one_producers_setting_makes_exactly_its_artifacts_stale(env):
    """The phase's done-when, first half: transcripts go stale, nothing else does."""
    client, headers, *_ = env
    sha = _sha()
    clip = _ingest_with(env, "lin/setting.mov", sha, {"proxy": _want(env, "proxy", sha)}, media_type="video")
    # Transcripts apply once the probe says how long it is.
    client.put(f"/v1/assets/{clip}/video-facet", json={"duration_sec": 5.0, "lineage": _want(env, "probe", sha)},
               headers=headers)
    r = client.post(f"/v1/assets/{clip}/transcript", json={
        "srt": "1\n00:00:00,000 --> 00:00:01,000\nhello\n", "source": "whisper",
        "lineage": _want(env, "transcript", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    assert _state(env, clip, "transcript") == "current" and _state(env, clip, "proxy") == "current"

    before = _producers(env)
    with _db(env) as s:
        s.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('producer.transcript',"
                       " '{\"model\": \"medium\"}', now())"))
        s.commit()
    try:
        after = _producers(env)
        assert _state(env, clip, "transcript") == "stale"
        assert _state(env, clip, "proxy") == "current"
        assert after["transcript"]["settings"]["model"] == "medium"
        assert after["transcript"]["counts"]["stale"] == before["transcript"]["counts"]["stale"] + 1
        for artifact in set(P.ARTIFACTS) - {"transcript"}:
            assert after[artifact]["counts"] == before[artifact]["counts"], artifact
    finally:
        with _db(env) as s:
            s.execute(text("DELETE FROM system_metadata WHERE key = 'producer.transcript'"))
            s.commit()


@pytest.mark.slow
def test_a_persons_transcript_is_current_whatever_the_settings(env):
    client, headers, *_ = env
    clip = _ingest(env, "lin/person.mov", media_type="video", sha=_sha())
    client.post(f"/v1/assets/{clip}/transcript", json={"srt": "1\n00:00:00,000 --> 00:00:01,000\nmine\n"},
                headers=headers)
    assert _row(env, clip, "transcript")[0] == P.PERSON and _state(env, clip, "transcript") == "current"
    # Deleting it is a person's choice too: nothing regenerates it.
    client.delete(f"/v1/assets/{clip}/transcript", headers=headers)
    assert _row(env, clip, "transcript")[0] == P.PERSON and _row(env, clip, "transcript")[4] == "empty"


@pytest.mark.slow
def test_nothing_found_is_a_result(env):
    client, headers, *_ = env
    sha = _sha()
    clip = _ingest_with(env, "lin/silent.mov", sha, None, media_type="video")
    client.post(f"/v1/assets/{clip}/transcript", json={"srt": "", "source": "whisper",
                                                       "lineage": _want(env, "transcript", sha)}, headers=headers)
    assert _row(env, clip, "transcript")[4] == "empty" and _state(env, clip, "transcript") == "current"
    face = _ingest_with(env, "lin/nofaces.jpg", sha, None)
    r = client.post("/v1/assets/batch-faces", json={"items": [{"asset_id": face, "faces": []}],
                                                    "lineage": _want(env, "faces", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    assert _row(env, face, "faces")[4] == "empty"


@pytest.mark.slow
def test_each_kind_of_write_records_its_artifact(env):
    client, headers, *_ = env
    sha = _sha()
    img = _ingest_with(env, "lin/all.jpg", sha, None)
    vid = _ingest_with(env, "lin/all.mov", sha, None, media_type="video")
    vec = [0.0] * 512
    writes = [
        ("vision", img, lambda: client.post(f"/v1/assets/{img}/vision", json={
            "model_id": "m", "description": "d", "lineage": _want(env, "vision", sha)}, headers=headers)),
        ("ocr", img, lambda: client.post(f"/v1/assets/{img}/ocr", json={
            "ocr_text": "EXIT", "lineage": _want(env, "ocr", sha)}, headers=headers)),
        ("clip", img, lambda: client.post(f"/v1/assets/{img}/embeddings", json={
            "model_id": "clip", "model_version": "ViT-B-32-openai", "vector": vec,
            "lineage": _want(env, "clip", sha)}, headers=headers)),
        ("faces", img, lambda: client.post(f"/v1/assets/{img}/faces", json={
            "faces": [], "lineage": _want(env, "faces", sha)}, headers=headers)),
        ("probe", vid, lambda: client.put(f"/v1/assets/{vid}/video-facet", json={
            "duration_sec": 12.0, "lineage": _want(env, "probe", sha)}, headers=headers)),
        ("analysis_proxy", vid, lambda: client.post(
            f"/v1/assets/{vid}/artifacts/analysis_proxy",
            files={"file": ("a.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42 x"), "video/mp4")},
            data={"lineage": json.dumps(_want(env, "analysis_proxy", sha))}, headers=headers)),
        ("video_preview", vid, lambda: client.post(
            f"/v1/assets/{vid}/artifacts/video_preview",
            files={"file": ("p.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42 x"), "video/mp4")},
            data={"lineage": json.dumps(_want(env, "video_preview", sha))}, headers=headers)),
    ]
    for artifact, clip, write in writes:
        r = write()
        assert r.status_code in (200, 201), (artifact, r.text)
        assert _state(env, clip, artifact) == "current", artifact
    # The probe's response is the facet itself, never the lineage.
    assert "lineage" not in client.put(f"/v1/assets/{vid}/video-facet", json={"duration_sec": 12.0},
                                       headers=headers).json()


@pytest.mark.slow
def test_scenes_count_once_every_scene_is_described(env):
    from src.server.repository import lineage as L

    client, headers, *_ = env
    sha = _sha()
    vid = _ingest_with(env, "lin/scenes.mov", sha, None, media_type="video")
    with _db(env) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30, video_indexed = true WHERE asset_id = :a"), {"a": vid})
        for i in range(2):
            s.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                           " created_at) VALUES (:i, :a, :n, :s, :e, :s, now())"),
                      {"i": f"scn_{vid}_{i}", "a": vid, "n": i, "s": i * 1000, "e": i * 1000 + 999})
        s.commit()
    r = client.patch(f"/v1/video/scenes/scn_{vid}_0", json={
        "model_id": "m", "model_version": "1", "description": "a beach", "tags": [],
        "lineage": _want(env, "scene_vision", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    with _db(env) as s:
        want = L.desired(s, "scene_vision")
        missing_before = L.counts(s, "scene_vision", want)["missing"]
    client.patch(f"/v1/video/scenes/scn_{vid}_1", json={
        "model_id": "m", "model_version": "1", "description": "a sunset", "tags": [],
        "lineage": _want(env, "scene_vision", sha)}, headers=headers)
    with _db(env) as s:
        assert L.counts(s, "scene_vision", want)["missing"] == missing_before - 1


# ---------------------------------------------------------------------------
# Failures are remembered
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_failure_is_kept_and_tried_again_later_and_later(env):
    from datetime import timedelta

    from src.server.repository import lineage as L
    from src.shared.utils import utcnow

    clip = _ingest(env, "lin/fails.mov", media_type="video", sha=_sha())
    waits = []
    with _db(env) as s:
        s.execute(text("UPDATE assets SET duration_sec = 5 WHERE asset_id = :a"), {"a": clip})
        s.commit()
        for _ in range(3):
            L.record_failure(s, clip, "transcript", "ffmpeg: no audio stream")
            retry_at, attempts = s.execute(text(
                "SELECT retry_at, attempts FROM artifact_lineage WHERE asset_id = :a AND artifact = 'transcript'"
            ), {"a": clip}).one()
            waits.append(retry_at - utcnow())
        assert attempts == 3
        assert timedelta(minutes=4) < waits[0] < timedelta(minutes=6)
        assert timedelta(minutes=19) < waits[2] < timedelta(minutes=21)
        assert L.counts(s, "transcript", L.desired(s, "transcript"))["failing"] >= 1
        # A success clears it.
        L.record(s, clip, "transcript", None)
        assert s.execute(text("SELECT error, attempts FROM artifact_lineage WHERE asset_id = :a"
                              " AND artifact = 'transcript'"), {"a": clip}).one() == (None, 0)


# ---------------------------------------------------------------------------
# Review fixes: only the server says a person made it; only CLIP's vectors are
# CLIP's; a form field's lineage is checked like a body's.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_client_cant_claim_a_person_made_it(env):
    client, headers, *_ = env
    sha = _sha()
    img = _ingest_with(env, "lin/claims-person.jpg", sha, None)
    r = client.post(f"/v1/assets/{img}/vision", json={
        "model_id": "m", "description": "d",
        "lineage": {"producer": P.PERSON, "version": "", "settings_hash": "", "source_sha256": sha}}, headers=headers)
    assert r.status_code == 200, r.text
    assert _row(env, img, "vision")[0] == P.UNKNOWN

    vid = _ingest_with(env, "lin/claims-person.mov", sha, None, media_type="video")
    client.post(f"/v1/assets/{vid}/transcript", json={
        "srt": "1\n00:00:00,000 --> 00:00:01,000\nhi\n", "source": "whisper",
        "lineage": {"producer": P.PERSON, "version": "", "settings_hash": ""}}, headers=headers)
    assert _row(env, vid, "transcript")[0] == P.UNKNOWN
    # A transcript a person typed is theirs.
    client.post(f"/v1/assets/{vid}/transcript", json={"srt": "1\n00:00:00,000 --> 00:00:01,000\nmine\n"},
                headers=headers)
    assert _row(env, vid, "transcript")[0] == P.PERSON


@pytest.mark.slow
def test_another_models_vectors_leave_clips_lineage_alone(env):
    client, headers, *_ = env
    sha = _sha()
    img = _ingest_with(env, "lin/featureprint.jpg", sha, None)
    r = client.post(f"/v1/assets/{img}/embeddings", json={
        "model_id": "clip", "model_version": "ViT-B-32-openai", "vector": [0.0] * 512,
        "lineage": _want(env, "clip", sha)}, headers=headers)
    assert r.status_code == 201, r.text
    r = client.post("/v1/assets/batch-embeddings", json={"items": [{
        "asset_id": img, "model_id": "apple_vision", "model_version": "1", "vector": [0.0] * 768}]}, headers=headers)
    assert r.status_code == 200, r.text
    assert _state(env, img, "clip") == "current"


@pytest.mark.slow
def test_only_clips_vectors_count_as_made(env):
    from src.server.repository import lineage as L

    client, headers, *_ = env
    lib = client.post("/v1/libraries", json={"name": "LinFP", "root_path": "/tmp/LinFP"}, headers=headers).json()
    img = _ingest_with((client, headers, lib["library_id"]), "fp.jpg", _sha(), None)
    client.post("/v1/assets/batch-embeddings", json={"items": [{
        "asset_id": img, "model_id": "apple_vision", "model_version": "1", "vector": [0.0] * 768}]}, headers=headers)
    with _db(env) as s:
        assert L.counts(s, "clip", L.desired(s, "clip"), lib["library_id"])["missing"] == 1


@pytest.mark.slow
def test_a_form_fields_lineage_is_checked(env):
    client, headers, *_ = env
    sha = _sha()
    vid = _ingest_with(env, "lin/badform.mov", sha, None, media_type="video")
    for bad in ({"producer": "analysis-proxy", "version": "1", "settings_hash": "x", "source_sha256": 12},
                {"producer": "analysis-proxy", "version": "1" * 500, "settings_hash": "x"}, ["not", "a", "dict"]):
        r = client.post(f"/v1/assets/{vid}/artifacts/analysis_proxy",
                        files={"file": ("a.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42 x"), "video/mp4")},
                        data={"lineage": json.dumps(bad)}, headers=headers)
        assert r.status_code in (200, 201), r.text
        assert _row(env, vid, "analysis_proxy")[0] == P.UNKNOWN


@pytest.mark.slow
def test_an_ingest_records_only_what_it_stored(env):
    from PIL import Image

    client, headers, library_id, *_ = env
    sha = _sha()
    buf = io.BytesIO()
    Image.new("RGB", (64, 36)).save(buf, format="JPEG")
    buf.seek(0)
    r = client.post("/v1/ingest", data={
        "library_id": library_id, "rel_path": "lin/nothing-stored.jpg", "file_size": "1000", "media_type": "image",
        "width": "64", "height": "36", "exif": json.dumps({"sha256": sha}),
        "vision": json.dumps({"model_id": "", "description": "dropped"}), "embeddings": json.dumps([]),
        "lineage": json.dumps({"vision": _want(env, "vision", sha), "clip": _want(env, "clip", sha)}),
    }, files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=headers)
    assert r.status_code == 200, r.text
    clip = r.json()["asset_id"]
    assert _row(env, clip, "vision") is None and _row(env, clip, "clip") is None
