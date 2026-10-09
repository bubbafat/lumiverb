# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Lineage on every write, and what's current (ADR-016 phase 3, piece 1).

A write records how its artifact was made: producer, version, settings
hash, source SHA-256. GET /v1/producers says, per producer, how many clips'
artifacts are current, stale, missing or failing: stale when any of the four
differs from what's registered now. A machine write that doesn't say is
refused, before anything is saved (Robert, Oct 9: the API doesn't allow
it). A person's transcript is a person's: current.
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


# Send no lineage at all: the write doesn't say how it was made.
SAYS_NOTHING = object()


def _ingest_post(env, rel_path: str, sha: str, lineage: dict | None | object = None, media_type: str = "image",
                 **fields: str):
    """POST /v1/ingest. lineage is by kind; None: a current worker's proxy,
    SAYS_NOTHING: none. fields: more form fields (vision, embeddings, video_facet)."""
    from PIL import Image

    client, headers, library_id, *_ = env
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color=(10, 20, 30)).save(buf, format="JPEG")
    buf.seek(0)
    data = {"library_id": library_id, "rel_path": rel_path, "file_size": "1000", "media_type": media_type,
            "width": "64", "height": "36", "exif": json.dumps({"sha256": sha}), **fields}
    if lineage is None:
        lineage = {"proxy": _want(env, "proxy", sha)}
    if lineage is not SAYS_NOTHING:
        data["lineage"] = json.dumps(lineage)
    return client.post("/v1/ingest", data=data, files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=headers)


def _ingest_with(env, rel_path: str, sha: str, lineage: dict | None = None, media_type: str = "image") -> str:
    """Ingest a clip whose proxy is a current worker's (None), or made as lineage (by kind) says."""
    r = _ingest_post(env, rel_path, sha, lineage, media_type)
    assert r.status_code == 200, r.text
    return r.json()["asset_id"]


def _refused(r, code: str = "lineage_required") -> None:
    """A machine write that doesn't say how it was made (or says another kind's producer made it)."""
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code, r.text


def _clips_at(env, rel_path: str) -> int:
    """How many clips the library keeps at the path."""
    library_id = env[2]
    with _db(env) as s:
        return s.execute(text("SELECT count(*) FROM assets WHERE library_id = :l AND rel_path = :p"),
                         {"l": library_id, "p": rel_path}).scalar()


# Everything kept about a clip, besides its row.
_CLIP_TABLES = ("artifact_lineage", "asset_metadata", "asset_ocr", "asset_embeddings", "faces", "video_facets",
                "video_scenes", "video_index_chunks")


def _everything(env, asset_id: str) -> dict:
    """All that's kept about a clip: its row and its rows in every table of what's made for it."""
    with _db(env) as s:
        out = {"assets": [dict(s.execute(text("SELECT * FROM assets WHERE asset_id = :a"),
                                         {"a": asset_id}).mappings().one())]}
        for table in _CLIP_TABLES:
            rows = s.execute(text(f"SELECT * FROM {table} WHERE asset_id = :a"), {"a": asset_id}).mappings()
            out[table] = sorted((dict(r) for r in rows), key=repr)
    return out


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
    assert set(t["counts"]) == {"applicable", "current", "stale", "missing", "failing", "given_up"}
    assert producers["clip"]["uniform"] is True and producers["transcript"]["uniform"] is False
    assert _producers(env, counts="false")["vision"]["counts"] is None


# ---------------------------------------------------------------------------
# Every write records lineage
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_an_ingest_that_says_how_its_proxy_was_made_is_current(env):
    sha = _sha()
    clip = _ingest_with(env, "lin/current.jpg", sha, {"proxy": _want(env, "proxy", sha)})
    assert _row(env, clip, "proxy")[0] == "proxy" and _state(env, clip, "proxy") == "current"


@pytest.mark.slow
def test_an_ingest_that_doesnt_say_how_its_proxy_was_made_is_refused(env):
    sha = _sha()
    _refused(_ingest_post(env, "lin/unknown.jpg", sha, SAYS_NOTHING))
    # Saying how something else was made isn't saying how the proxy was.
    _refused(_ingest_post(env, "lin/unknown.jpg", sha, {"vision": _want(env, "vision", sha)}))
    _refused(_ingest_post(env, "lin/unknown.jpg", sha, {"proxy": None}))
    assert _clips_at(env, "lin/unknown.jpg") == 0  # nothing was saved


@pytest.mark.slow
def test_another_kinds_producer_cant_make_this_one(env):
    sha = _sha()
    _refused(_ingest_post(env, "lin/wrongkind.jpg", sha, {"proxy": _want(env, "faces", sha)}),
             "lineage_wrong_producer")
    assert _clips_at(env, "lin/wrongkind.jpg") == 0

    clip = _ingest_with(env, "lin/wrongkind.jpg", sha)
    before = _everything(env, clip)
    client, headers, *_ = env
    _refused(client.post(f"/v1/assets/{clip}/vision", json={
        "model_id": "m", "description": "d", "lineage": _want(env, "ocr", sha)}, headers=headers),
        "lineage_wrong_producer")
    assert _everything(env, clip) == before


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
                       " '{\"vad_min_silence_ms\": 700, \"model\": \"medium\"}', now())"))
        s.commit()
    try:
        after = _producers(env)
        assert _state(env, clip, "transcript") == "stale"
        assert _state(env, clip, "proxy") == "current"
        # The model is the transcripts job's (Settings → AI), whatever an override says.
        assert after["transcript"]["settings"] == {"model": "small", "vad_min_silence_ms": 700}
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
    client.post(f"/v1/assets/{clip}/transcript", json={"source": "manual", "srt": "1\n00:00:00,000 --> 00:00:01,000\nmine\n"},
                headers=headers)
    assert _row(env, clip, "transcript")[0] == P.PERSON and _state(env, clip, "transcript") == "current"
    # Removing theirs leaves none, and it's missing again: the machine makes one (Robert, Oct 9).
    client.delete(f"/v1/assets/{clip}/transcript", params={"which": "manual"}, headers=headers)
    assert _row(env, clip, "transcript") is None and _state(env, clip, "transcript") == "missing"


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
    r = client.put(f"/v1/assets/{vid}/video-facet", json={"duration_sec": 12.0, "lineage": _want(env, "probe", sha)},
                   headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["duration_sec"] == 12.0 and "lineage" not in r.json()


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
    before = _everything(env, img)
    r = client.post(f"/v1/assets/{img}/vision", json={
        "model_id": "m", "description": "d",
        "lineage": {"producer": P.PERSON, "version": "", "settings_hash": "", "source_sha256": sha}}, headers=headers)
    _refused(r, "lineage_wrong_producer")
    assert _everything(env, img) == before and _row(env, img, "vision") is None

    vid = _ingest_with(env, "lin/claims-person.mov", sha, None, media_type="video")
    before = _everything(env, vid)
    _refused(client.post(f"/v1/assets/{vid}/transcript", json={
        "srt": "1\n00:00:00,000 --> 00:00:01,000\nhi\n", "source": "whisper",
        "lineage": {"producer": P.PERSON, "version": "", "settings_hash": ""}}, headers=headers),
        "lineage_wrong_producer")
    assert _everything(env, vid) == before and _row(env, vid, "transcript") is None
    # A transcript a person typed is theirs.
    client.post(f"/v1/assets/{vid}/transcript", json={"source": "manual", "srt": "1\n00:00:00,000 --> 00:00:01,000\nmine\n"},
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
    before = _everything(env, vid)
    for bad in ({"producer": "analysis-proxy", "version": "1", "settings_hash": "x", "source_sha256": 12},
                {"producer": "analysis-proxy", "version": "1" * 500, "settings_hash": "x"}, ["not", "a", "dict"]):
        r = client.post(f"/v1/assets/{vid}/artifacts/analysis_proxy",
                        files={"file": ("a.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42 x"), "video/mp4")},
                        data={"lineage": json.dumps(bad)}, headers=headers)
        _refused(r)  # unreadable is the same as not saying
        assert _everything(env, vid) == before and _row(env, vid, "analysis_proxy") is None
    r = client.post(f"/v1/assets/{vid}/artifacts/analysis_proxy",
                    files={"file": ("a.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42 x"), "video/mp4")},
                    data={"lineage": "not json"}, headers=headers)
    _refused(r)
    assert _everything(env, vid) == before


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
        "lineage": json.dumps({"proxy": _want(env, "proxy", sha), "vision": _want(env, "vision", sha),
                               "clip": _want(env, "clip", sha)}),
    }, files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=headers)
    assert r.status_code == 200, r.text
    clip = r.json()["asset_id"]
    assert _row(env, clip, "vision") is None and _row(env, clip, "clip") is None


# ---------------------------------------------------------------------------
# Every machine write says how it was made (Robert, Oct 9: the API doesn't
# allow one that doesn't): refused, and nothing is saved.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_every_machine_write_that_doesnt_say_is_refused_and_saves_nothing(env):
    from PIL import Image

    client, headers, *_ = env
    sha = _sha()
    img = _ingest_with(env, "lin/refused.jpg", sha)
    vid = _ingest_with(env, "lin/refused.mov", sha, media_type="video")
    # The video has a scene to describe and a chunk of scene finding claimed, as a worker finds them.
    with _db(env) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30 WHERE asset_id = :a"), {"a": vid})
        s.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                       " created_at) VALUES (:i, :a, 0, 0, 999, 0, now())"), {"i": f"scn_{vid}_0", "a": vid})
        s.commit()
    assert client.post(f"/v1/video/{vid}/chunks", json={"duration_sec": 30}, headers=headers).status_code == 200
    chunk = client.get(f"/v1/video/{vid}/chunks/next", headers=headers).json()

    def mp4() -> tuple:
        return ("v.mp4", io.BytesIO(b"\x00\x00\x00\x18ftypmp42 x"), "video/mp4")

    def jpeg() -> tuple:
        buf = io.BytesIO()
        Image.new("RGB", (64, 36)).save(buf, format="JPEG")
        return ("p.jpg", io.BytesIO(buf.getvalue()), "image/jpeg")

    vec = [0.0] * 512
    face = {"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, "detection_confidence": 0.9, "embedding": vec}
    scene = {"scene_index": 0, "start_ms": 0, "end_ms": 999, "rep_frame_ms": 0}
    writes = [
        ("probe", vid, lambda: client.put(f"/v1/assets/{vid}/video-facet", json={"duration_sec": 12.0},
                                          headers=headers)),
        ("vision", img, lambda: client.post(f"/v1/assets/{img}/vision", json={
            "model_id": "m", "description": "d"}, headers=headers)),
        ("vision", img, lambda: client.post("/v1/assets/batch-vision", json={
            "items": [{"asset_id": img, "model_id": "m", "description": "d"}]}, headers=headers)),
        ("ocr", img, lambda: client.post(f"/v1/assets/{img}/ocr", json={"ocr_text": "EXIT"}, headers=headers)),
        ("ocr", img, lambda: client.post("/v1/assets/batch-ocr", json={
            "items": [{"asset_id": img, "ocr_text": "EXIT"}]}, headers=headers)),
        ("clip", img, lambda: client.post(f"/v1/assets/{img}/embeddings", json={
            "model_id": P.CLIP_MODEL_ID, "model_version": "ViT-B-32-openai", "vector": vec}, headers=headers)),
        ("clip", img, lambda: client.post("/v1/assets/batch-embeddings", json={"items": [{
            "asset_id": img, "model_id": P.CLIP_MODEL_ID, "model_version": "ViT-B-32-openai", "vector": vec}]},
            headers=headers)),
        ("faces", img, lambda: client.post(f"/v1/assets/{img}/faces", json={"faces": [face]}, headers=headers)),
        ("faces", img, lambda: client.post("/v1/assets/batch-faces", json={
            "items": [{"asset_id": img, "faces": [face]}]}, headers=headers)),
        ("transcript", vid, lambda: client.post(f"/v1/assets/{vid}/transcript", json={
            "srt": "1\n00:00:00,000 --> 00:00:01,000\nhello\n", "source": "whisper"}, headers=headers)),
        ("proxy", img, lambda: client.post(f"/v1/assets/{img}/artifacts/proxy", files={"file": jpeg()},
                                           headers=headers)),
        ("video_preview", vid, lambda: client.post(f"/v1/assets/{vid}/artifacts/video_preview",
                                                   files={"file": mp4()}, headers=headers)),
        ("analysis_proxy", vid, lambda: client.post(f"/v1/assets/{vid}/artifacts/analysis_proxy",
                                                    files={"file": mp4()}, headers=headers)),
        ("proxy", vid, lambda: client.post(f"/v1/assets/{vid}/artifacts", files={
            "proxy": jpeg(), "video_preview": mp4()}, headers=headers)),
        # Saying how the preview was made isn't saying how the proxy was.
        ("proxy", vid, lambda: client.post(f"/v1/assets/{vid}/artifacts", files={
            "proxy": jpeg(), "video_preview": mp4()},
            data={"lineage": json.dumps({"video_preview": _want(env, "video_preview", sha)})}, headers=headers)),
        # Scanned again with new content, and ingested again.
        ("proxy", img, lambda: _ingest_post(env, "lin/refused.jpg", _sha(), SAYS_NOTHING)),
        ("proxy", img, lambda: client.post(f"/v1/assets/{img}/ingest", files={"proxy": jpeg()}, headers=headers)),
        # An ingest says how it made each thing it stores, not just the proxy.
        ("vision", img, lambda: client.post(f"/v1/assets/{img}/ingest", files={"proxy": jpeg()}, data={
            "vision": json.dumps({"model_id": "m", "description": "d"}),
            "lineage": json.dumps({"proxy": _want(env, "proxy", sha)})}, headers=headers)),
        ("clip", img, lambda: _ingest_post(env, "lin/refused.jpg", sha, {"proxy": _want(env, "proxy", sha)},
                                           embeddings=json.dumps([{"model_id": P.CLIP_MODEL_ID,
                                                                   "model_version": "ViT-B-32-openai",
                                                                   "vector": vec}]))),
        ("probe", vid, lambda: _ingest_post(env, "lin/refused.mov", sha, {"proxy": _want(env, "proxy", sha)},
                                            media_type="video", video_facet=json.dumps({"duration_sec": 12.0}))),
        ("scenes", vid, lambda: client.post(f"/v1/video/chunks/{chunk['chunk_id']}/complete", json={
            "worker_id": chunk["worker_id"], "scenes": [scene], "next_anchor_phash": None,
            "next_scene_start_ms": None}, headers=headers)),
        ("scene_vision", vid, lambda: client.patch(f"/v1/video/scenes/scn_{vid}_0", json={
            "model_id": "m", "model_version": "1", "description": "a beach", "tags": []}, headers=headers)),
    ]
    for artifact, clip, write in writes:
        before = _everything(env, clip)
        r = write()
        assert r.status_code == 422 and r.json()["error"]["code"] == "lineage_required", (artifact, r.text)
        assert r.json()["error"]["details"]["artifact"] == artifact, r.text
        assert _everything(env, clip) == before, artifact

    # A new clip that doesn't say how its proxy was made isn't kept at all.
    _refused(_ingest_post(env, "lin/refused-new.jpg", sha, SAYS_NOTHING))
    assert _clips_at(env, "lin/refused-new.jpg") == 0

    # A person's writes need none.
    r = client.post(f"/v1/assets/{vid}/transcript", json={"source": "manual", "srt": "1\n00:00:00,000 --> 00:00:01,000\nmine\n"},
                    headers=headers)
    assert r.status_code == 200, r.text
    assert _row(env, vid, "transcript")[0] == P.PERSON
    r = client.patch(f"/v1/assets/{img}/corrections", json={"description": "my dog", "tags": ["dog"]},
                     headers=headers)
    assert r.status_code == 200, r.text
    # Another model's vectors aren't the CLIP producer's artifact: nothing to say.
    r = client.post(f"/v1/assets/{img}/embeddings", json={
        "model_id": "apple_vision", "model_version": "1", "vector": [0.0] * 768}, headers=headers)
    assert r.status_code == 201, r.text
