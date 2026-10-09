# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The reconciler: what each clip still needs, from what exists and how it
was made (ADR-016 phase 3, piece 2).

An artifact is missing when it doesn't exist, whatever lineage says. One
that exists is current or stale by its lineage; with none it's an unknown
producer's, so stale. The worker is handed what's missing, and what was
made from a file whose content has since changed; stale from a settings or
producer change waits for approval (piece 5). A failure waits its turn
(5 minutes, doubling to a day) and is reported by the worker. The repair
summary, the page filters and the library health dot all say the same.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from src.shared import producers as P
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want


def _library(env, name: str) -> tuple:
    client, headers, *_ = env
    r = client.post("/v1/libraries", json={"name": name, "root_path": f"/tmp/{name}"}, headers=headers)
    assert r.status_code == 200, r.text
    return (client, headers, r.json()["library_id"], *env[3:])


def _summary(env) -> dict:
    client, headers, library_id, *_ = env
    r = client.get("/v1/assets/repair-summary", params={"library_id": library_id}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _due(env, flag: str) -> list[str]:
    """What the worker is handed for one step: the page filter it uses."""
    client, headers, library_id, *_ = env
    r = client.get("/v1/assets/page", params={"library_id": library_id, flag: "true"}, headers=headers)
    assert r.status_code == 200, r.text
    return [i["asset_id"] for i in r.json()["items"]]


def _counts(env, artifact: str) -> dict:
    client, headers, library_id, *_ = env
    r = client.get("/v1/producers", params={"library_id": library_id}, headers=headers)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p["counts"] for p in r.json()["producers"]}[artifact]


def _pending(env) -> int:
    client, headers, library_id, *_ = env
    r = client.get("/v1/libraries/health", headers=headers)
    assert r.status_code == 200, r.text
    return {x["library_id"]: x["pending"] for x in r.json()}[library_id]


def _describe(env, clip: str, sha: str | None, lineage: bool = True):
    client, headers, *_ = env
    body = {"model_id": "m", "description": "a dog on a beach"}
    if lineage:
        body["lineage"] = _want(env, "vision", sha)
    r = client.post(f"/v1/assets/{clip}/vision", json=body, headers=headers)
    assert r.status_code == 200, r.text


def _fail(env, items: list[dict], headers: dict | None = None):
    client, own, *_ = env
    return client.post("/v1/producers/failures", json={"items": items}, headers=headers or own)


# ---------------------------------------------------------------------------
# Missing is what doesn't exist; stale is what exists, made otherwise
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_missing_until_made_then_current(env):
    lib = _library(env, "RecMade")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, {"proxy": _want(env, "proxy", sha)})
    assert _due(lib, "missing_vision") == [clip]
    assert _summary(lib)["missing_vision"] == 1
    assert _counts(lib, "vision") == {"applicable": 1, "current": 0, "stale": 0, "missing": 1, "failing": 0,
                                      "given_up": 0}

    _describe(lib, clip, sha)
    assert _due(lib, "missing_vision") == []
    assert _summary(lib)["missing_vision"] == 0
    assert _counts(lib, "vision")["current"] == 1


@pytest.mark.slow
def test_lineage_without_the_artifact_is_missing(env):
    lib = _library(env, "RecGone")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    _describe(lib, clip, sha)
    with _db(env) as s:  # the description went, its lineage stayed
        s.execute(text("DELETE FROM asset_metadata WHERE asset_id = :a"), {"a": clip})
        s.commit()
    assert _due(lib, "missing_vision") == [clip]
    assert _counts(lib, "vision")["missing"] == 1


@pytest.mark.slow
def test_an_artifact_nobody_said_how_was_made_is_stale_not_missing(env):
    lib = _library(env, "RecUnknown")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    _describe(lib, clip, sha, lineage=False)
    with _db(env) as s:  # and no lineage row at all
        s.execute(text("DELETE FROM artifact_lineage WHERE asset_id = :a AND artifact = 'vision'"), {"a": clip})
        s.commit()
    c = _counts(lib, "vision")
    assert (c["stale"], c["missing"], c["current"]) == (1, 0, 0)
    # Stale from how it was made waits for approval: not handed out.
    assert _due(lib, "missing_vision") == []


@pytest.mark.slow
def test_a_settings_change_makes_it_stale_and_it_waits_for_approval(env):
    lib = _library(env, "RecSettings")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    _describe(lib, clip, sha)
    with _db(env) as s:
        s.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('producer.vision', :v, now())"
                       " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                  {"v": '{"temperature": 0.5}'})
        s.commit()
    try:
        assert _counts(lib, "vision")["stale"] == 1
        assert _due(lib, "missing_vision") == []
        assert _summary(lib)["missing_vision"] == 0
    finally:
        with _db(env) as s:
            s.execute(text("DELETE FROM system_metadata WHERE key = 'producer.vision'"))
            s.commit()


# ---------------------------------------------------------------------------
# A changed file: what was made from the old content is due again
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_changed_file_is_described_again_without_asking(env):
    client, headers, *_ = env
    lib = _library(env, "RecChanged")
    old, new = _sha(), _sha()
    clip = _ingest_with(lib, "a.jpg", old, None)
    _describe(lib, clip, old)
    r = client.post(f"/v1/assets/{clip}/embeddings", json={
        "model_id": "clip", "model_version": "ViT-B-32-openai", "vector": [0.0] * 512,
        "lineage": _want(env, "clip", old)}, headers=headers)
    assert r.status_code == 201, r.text
    assert _due(lib, "missing_vision") == [] and _due(lib, "missing_embeddings") == []

    assert _ingest_with(lib, "a.jpg", new, None) == clip  # the file was replaced
    assert _due(lib, "missing_vision") == [clip] and _due(lib, "missing_embeddings") == [clip]
    assert _summary(lib)["missing_vision"] == 1
    assert _counts(lib, "vision")["stale"] == 1  # it still has its old description meanwhile

    _describe(lib, clip, new)
    assert _due(lib, "missing_vision") == []
    assert _counts(lib, "vision")["current"] == 1


@pytest.mark.slow
def test_a_changed_video_loses_its_scenes(env):
    client, headers, *_ = env
    lib = _library(env, "RecScenes")
    old = _sha()
    vid = _ingest_with(lib, "a.mov", old, None, media_type="video")
    with _db(env) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30, video_indexed = true WHERE asset_id = :a"), {"a": vid})
        s.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                       " description, created_at) VALUES (:i, :a, 0, 0, 999, 0, 'a beach', now())"),
                  {"i": f"scn_{vid}_0", "a": vid})
        s.commit()
    assert _due(lib, "missing_video_scenes") == [] and _due(lib, "missing_scene_vision") == []

    _ingest_with(lib, "a.mov", _sha(), None, media_type="video")
    assert _due(lib, "missing_video_scenes") == [vid]
    with _db(env) as s:
        assert s.execute(text("SELECT count(*) FROM video_scenes WHERE asset_id = :a"), {"a": vid}).scalar() == 0
        assert s.execute(text("SELECT count(*) FROM artifact_lineage WHERE asset_id = :a"
                              " AND artifact IN ('scenes', 'scene_vision')"), {"a": vid}).scalar() == 0


@pytest.mark.slow
def test_a_persons_transcript_stays_when_the_file_changes(env):
    client, headers, *_ = env
    lib = _library(env, "RecPerson")
    vid = _ingest_with(lib, "a.mov", _sha(), None, media_type="video")
    with _db(env) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30 WHERE asset_id = :a"), {"a": vid})
        s.commit()
    r = client.post(f"/v1/assets/{vid}/transcript", json={"srt": "1\n00:00:00,000 --> 00:00:01,000\nmine\n"},
                    headers=headers)
    assert r.status_code == 200, r.text
    _ingest_with(lib, "a.mov", _sha(), None, media_type="video")
    assert _due(lib, "missing_transcription") == []


# ---------------------------------------------------------------------------
# Failures wait their turn
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_failure_waits_then_is_handed_out_again(env):
    lib = _library(env, "RecFail")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    r = _fail(env, [{"asset_id": clip, "artifact": "vision", "error": "the vision endpoint timed out"}])
    assert r.status_code == 200, r.text
    assert r.json() == {"recorded": 1}

    assert _due(lib, "missing_vision") == []
    assert _summary(lib)["missing_vision"] == 0
    assert _summary(lib)["waiting_failures"] == 1
    assert _counts(lib, "vision")["failing"] == 1
    assert _pending(lib) >= 1  # not healthy: something's failing

    with _db(env) as s:  # its turn comes
        s.execute(text("UPDATE artifact_lineage SET retry_at = now() - interval '1 second'"
                       " WHERE asset_id = :a AND artifact = 'vision'"), {"a": clip})
        s.commit()
    assert _due(lib, "missing_vision") == [clip]

    _fail(env, [{"asset_id": clip, "artifact": "vision", "error": "again"}])
    with _db(env) as s:
        attempts, wait = s.execute(text(
            "SELECT attempts, retry_at - now() FROM artifact_lineage WHERE asset_id = :a AND artifact = 'vision'"
        ), {"a": clip}).one()
    assert attempts == 2 and timedelta(minutes=9) < wait <= timedelta(minutes=10)

    _describe(lib, clip, sha)  # made at last: the failure is behind it
    assert _counts(lib, "vision") == {"applicable": 1, "current": 1, "stale": 0, "missing": 0, "failing": 0,
                                      "given_up": 0}


@pytest.mark.slow
def test_reporting_failures_needs_an_editor(env):
    from tests.test_archive_trash_safety import _key_with_role

    lib = _library(env, "RecFailRole")
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    viewer = _key_with_role(env, "viewer")
    assert _fail(env, [{"asset_id": clip, "artifact": "vision", "error": "x"}], headers=viewer).status_code == 403
    editor = _key_with_role(env, "editor")
    assert _fail(env, [{"asset_id": clip, "artifact": "vision", "error": "x"}], headers=editor).status_code == 200


@pytest.mark.slow
def test_a_failure_for_no_such_artifact_or_clip(env):
    lib = _library(env, "RecFailBad")
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    r = _fail(env, [{"asset_id": clip, "artifact": "smell", "error": "x"}])
    assert r.status_code == 422
    r = _fail(env, [{"asset_id": "ast_nope", "artifact": "vision", "error": "x"}])
    assert r.status_code == 200 and r.json() == {"recorded": 0}
    assert _fail(env, []).json() == {"recorded": 0}
    assert _fail(env, [{"asset_id": clip, "artifact": "vision", "error": "x"}] * 501).status_code == 422


@pytest.mark.slow
def test_every_producer_sorts_a_new_clip_the_same_everywhere(env):
    """The summary, page filters and producer counts agree for each step the worker runs."""
    lib = _library(env, "RecAgree")
    img = _ingest_with(lib, "a.jpg", _sha(), None)
    vid = _ingest_with(lib, "a.mov", _sha(), None, media_type="video")
    with _db(env) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30 WHERE asset_id = :a"), {"a": vid})
        s.commit()
    summary = _summary(lib)
    for flag, artifact in P.MISSING_FLAGS.items():
        due = _due(lib, flag)
        assert len(due) == summary[flag], flag
        assert _counts(lib, artifact)["missing"] == len(due), flag
    assert set(_due(lib, "missing_vision")) == {img}
    assert set(_due(lib, "missing_transcription")) == {vid}


# ---------------------------------------------------------------------------
# Review fixes: unknown isn't changed; a replaced file resets its scenes on
# either ingest; scenes are never handed out in place; waiting failures are
# what the counts leave out.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_made_before_the_file_had_a_hash_is_stale_not_redone(env):
    client, headers, *_ = env
    lib = _library(env, "RecNoSha")
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    with _db(env) as s:  # made while the file's SHA-256 wasn't known
        s.execute(text("UPDATE assets SET sha256 = NULL WHERE asset_id = :a"), {"a": clip})
        s.commit()
    _describe(lib, clip, None)
    with _db(env) as s:
        s.execute(text("UPDATE artifact_lineage SET source_sha256 = NULL WHERE asset_id = :a"), {"a": clip})
        s.execute(text("UPDATE assets SET sha256 = :s WHERE asset_id = :a"), {"s": _sha(), "a": clip})
        s.commit()
    assert _due(lib, "missing_vision") == []
    assert _counts(lib, "vision")["stale"] == 1


@pytest.mark.slow
def test_a_file_replaced_through_the_other_ingest_loses_its_scenes_too(env):
    import io
    import json

    from PIL import Image

    client, headers, *_ = env
    lib = _library(env, "RecOtherIngest")
    vid = _ingest_with(lib, "a.mov", _sha(), None, media_type="video")
    with _db(env) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30, video_indexed = true WHERE asset_id = :a"), {"a": vid})
        s.execute(text("INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                       " description, created_at) VALUES (:i, :a, 0, 0, 999, 0, 'a beach', now())"),
                  {"i": f"scn_{vid}_0", "a": vid})
        s.commit()
    buf = io.BytesIO()
    Image.new("RGB", (64, 36)).save(buf, format="JPEG")
    buf.seek(0)
    r = client.post(f"/v1/assets/{vid}/ingest", data={"exif": json.dumps({"sha256": _sha()})},
                    files={"proxy": ("p.jpg", buf, "image/jpeg")}, headers=headers)
    assert r.status_code == 200, r.text
    assert _due(lib, "missing_video_scenes") == [vid]
    with _db(env) as s:
        assert s.execute(text("SELECT count(*) FROM video_scenes WHERE asset_id = :a"), {"a": vid}).scalar() == 0


@pytest.mark.slow
def test_scenes_are_never_handed_out_to_be_redone_in_place(env):
    """Ingest resets them when a file is replaced; a hash changed any other
    way mustn't hand out a video whose scenes the worker can't redo."""
    lib = _library(env, "RecScenesInPlace")
    vid = _ingest_with(lib, "a.mov", _sha(), None, media_type="video")
    with _db(env) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30, video_indexed = true WHERE asset_id = :a"), {"a": vid})
        s.execute(text("INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
                       " source_sha256, produced_at, outcome, attempts) VALUES (:a, 'scenes', 'scene-detect', '1',"
                       " 'h', 'old', now(), 'ok', 0)"), {"a": vid})
        s.commit()
    assert _due(lib, "missing_video_scenes") == []
    assert _counts(lib, "scenes")["stale"] == 1


@pytest.mark.slow
def test_waiting_failures_are_what_the_counts_leave_out(env):
    lib = _library(env, "RecWaitCount")
    img = _ingest_with(lib, "a.jpg", _sha(), None)
    _fail(env, [{"asset_id": img, "artifact": "vision", "error": "x"},
                {"asset_id": img, "artifact": "transcript", "error": "x"},  # an image has no transcript
                {"asset_id": img, "artifact": "proxy", "error": "x"}])  # the scan's, not enrich's
    summary = _summary(lib)
    assert summary["waiting_failures"] == 1 and summary["missing_vision"] == 0
