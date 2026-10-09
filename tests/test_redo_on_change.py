# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Redo on change: a settings change is the approval (ADR-016 phase 4, Robert Oct 9).

What was made with another model or settings than now is made again, after
anything missing, everywhere: the scheduler's tier 4. Nobody approves an
upgrade any more: saving a new model in Settings → AI asks once, naming how
many clips it makes again. An admin can stop a producer's redo and resume
it; a new model for it resumes it. A person's work is never redone, and
producers that can't be made again in place yet (scenes, proxies,
previews) show as stale with the reason.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from src.server.repository import lineage
from src.server.scheduler.kinds import KINDS
from src.server.scheduler.queue import candidates
from src.server.scheduler.service import _job_models
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha, _want
from tests.test_reconciler import _counts, _library

pytestmark = pytest.mark.slow

OLD = "0ld5e771n65"  # a settings hash nothing makes now
VISION = "qwen3-vl:8b-instruct"
SRT = "1\n00:00:00,000 --> 00:00:01,000\nhello\n"


def _set_model(env, job: str, model: str) -> None:
    """The tenant's model for a job, straight in the control plane (no machine asked)."""
    from src.server.database import get_control_session
    from src.server.models.control_plane import Tenant
    from src.server.repository.ai_machines import set_job_model

    with get_control_session() as ctrl:
        tenant = ctrl.get(Tenant, env[4])
        set_job_model(tenant, job, model)
        ctrl.add(tenant)
        ctrl.commit()


@pytest.fixture(autouse=True)
def _vision_on(env):
    _set_model(env, "vision", VISION)
    yield


def _made_old(env, clip: str, sha: str, artifact: str = "vision") -> None:
    client, headers, *_ = env
    old = {"producer": artifact, "version": "1", "settings_hash": OLD, "source_sha256": sha}
    if artifact == "vision":
        r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "m", "description": "old words",
                                                            "lineage": old}, headers=headers)
    else:
        r = client.post(f"/v1/assets/{clip}/ocr", json={"ocr_text": "OLD SIGN", "lineage": old}, headers=headers)
    assert r.status_code == 200, r.text


def _stale_clip(lib, name: str, artifact: str = "vision") -> tuple[str, str]:
    sha = _sha()
    clip = _ingest_with(lib, name, sha, None)
    _made_old(lib, clip, sha, artifact)
    return clip, sha


def _due(env, kind: str) -> list[str]:
    """What the scheduler finds due for one kind in this test's library."""
    with _db(env) as s:
        k = KINDS[kind]
        want = lineage.desired(s, k.artifact, _job_models(env[4])) if k.redo else None
        return [i["asset_id"] for i in candidates(s, k, [env[2]], want=want)]


def _producer(env, artifact: str = "vision", headers: dict | None = None, **params) -> dict:
    client, own, *_ = env
    r = client.get("/v1/producers", params=params, headers=headers or own)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p for p in r.json()["producers"]}[artifact]


def _error(r) -> dict:
    return r.json()["error"]


# ---------------------------------------------------------------------------
# Stale is redone, after anything missing, with nobody approving
# ---------------------------------------------------------------------------


def test_a_clip_made_with_older_settings_is_redone_without_anyone_approving(env):
    lib = _library(env, "RedoStale")
    clip, sha = _stale_clip(lib, "a.jpg")
    assert _counts(lib, "vision")["stale"] == 1
    assert _due(lib, "redo_vision") == [clip]
    assert _due(lib, "vision") == []  # not missing


def test_a_clip_made_again_with_todays_settings_is_done(env):
    lib = _library(env, "RedoDone")
    client, headers, *_ = lib
    clip, sha = _stale_clip(lib, "a.jpg")
    r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "m", "description": "new words",
                                                        "lineage": _want(lib, "vision", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    assert _due(lib, "redo_vision") == []
    assert _counts(lib, "vision")["current"] == 1


def test_a_missing_clip_is_due_as_missing_not_as_a_redo(env):
    lib = _library(env, "RedoMissing")
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    assert _due(lib, "vision") == [clip]
    assert _due(lib, "redo_vision") == []


def test_a_clip_whose_file_changed_is_due_as_missing_not_as_a_redo(env):
    # Both would make it again; it must never be in hand twice.
    lib = _library(env, "RedoChanged")
    clip, sha = _stale_clip(lib, "a.jpg")
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET sha256 = :n WHERE asset_id = :a"), {"n": _sha(), "a": clip})
        s.commit()
    assert _due(lib, "vision") == [clip]
    assert _due(lib, "redo_vision") == []


def test_a_description_nobody_said_how_was_made_isnt_kept_so_its_made_not_redone(env):
    lib = _library(env, "RedoUnknown")
    client, headers, *_ = lib
    clip = _ingest_with(lib, "a.jpg", _sha(), None)
    r = client.post(f"/v1/assets/{clip}/vision", json={"model_id": "m", "description": "from the Mac"},
                    headers=headers)
    assert r.status_code == 422 and r.json()["error"]["code"] == "lineage_required", r.text
    with _db(lib) as s:
        assert s.execute(text("SELECT count(*) FROM asset_metadata WHERE asset_id = :a"), {"a": clip}).scalar() == 0
        assert s.execute(text("SELECT count(*) FROM artifact_lineage WHERE asset_id = :a AND artifact = 'vision'"),
                         {"a": clip}).scalar() == 0
    # Still missing: made like any other, never redone.
    assert _due(lib, "vision") == [clip]
    assert _due(lib, "redo_vision") == []


def test_a_failed_redo_waits_its_turn(env):
    lib = _library(env, "RedoFailed")
    client, headers, *_ = lib
    clip, _ = _stale_clip(lib, "a.jpg")
    r = client.post("/v1/producers/failures", json={"items": [
        {"asset_id": clip, "artifact": "vision", "error": "the model said nothing"}]}, headers=headers)
    assert r.status_code < 300, r.text
    assert _due(lib, "redo_vision") == []


def test_a_persons_transcript_is_never_redone(env):
    lib = _library(env, "RedoPerson")
    client, headers, *_ = lib
    vid = _ingest_with(lib, "t.mov", _sha(), None, media_type="video")
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30, analysis_proxy_key = 'k' WHERE asset_id = :a"),
                  {"a": vid})
        s.commit()
    r = client.post(f"/v1/assets/{vid}/transcript", json={"srt": SRT, "source": "manual"}, headers=headers)
    assert r.status_code == 200, r.text
    assert _due(lib, "redo_transcript") == []
    assert _due(lib, "transcript") == []


def test_a_machine_transcript_made_with_another_model_is_redone(env):
    lib = _library(env, "RedoTranscript")
    client, headers, *_ = lib
    sha = _sha()
    vid = _ingest_with(lib, "t.mov", sha, None, media_type="video")
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30, analysis_proxy_key = 'k' WHERE asset_id = :a"),
                  {"a": vid})
        s.commit()
    r = client.post(f"/v1/assets/{vid}/transcript", json={
        "srt": SRT, "source": "whisper",
        "lineage": {"producer": "whisper", "version": "1", "settings_hash": OLD, "source_sha256": sha}},
        headers=headers)
    assert r.status_code == 200, r.text
    assert _due(lib, "redo_transcript") == [vid]


# ---------------------------------------------------------------------------
# Scene descriptions: a video is redone until every scene is
# ---------------------------------------------------------------------------


def _video_with_scenes(lib, n: int = 2) -> tuple[str, str, list[str]]:
    sha = _sha()
    vid = _ingest_with(lib, "v.mov", sha, None, media_type="video")
    old = {"producer": "scene-vision", "version": "1", "settings_hash": OLD}
    scenes = [f"scn_{vid[-10:]}_{i}" for i in range(n)]
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30, video_indexed = true, analysis_proxy_key = 'k'"
                       " WHERE asset_id = :a"), {"a": vid})
        for i, scene_id in enumerate(scenes):
            s.execute(text(
                "INSERT INTO video_scenes (scene_id, asset_id, scene_index, start_ms, end_ms, rep_frame_ms,"
                " description, tags, lineage, created_at) VALUES (:s, :a, :i, :t, :t + 1000, :t, 'old words',"
                " '[]'::jsonb, CAST(:l AS jsonb), now())"), {"s": scene_id, "a": vid, "i": i, "t": i * 1000,
                                                            "l": json.dumps(old)})
        s.execute(text(
            "INSERT INTO artifact_lineage (asset_id, artifact, producer, producer_version, settings_hash,"
            " source_sha256, produced_at, outcome, attempts) VALUES (:a, 'scene_vision', 'scene-vision', '1', :h,"
            " :sha, now(), 'ok', 0)"), {"a": vid, "h": OLD, "sha": sha})
        s.commit()
    return vid, sha, scenes


def _describe_scene(env, scene_id: str, sha: str):
    client, headers, *_ = env
    r = client.patch(f"/v1/video/scenes/{scene_id}", json={
        "model_id": "m", "model_version": "1", "description": "new words", "tags": [],
        "lineage": _want(env, "scene_vision", sha)}, headers=headers)
    assert r.status_code == 200, r.text


def test_a_video_is_redone_until_every_scene_is_described_again(env):
    lib = _library(env, "RedoScenes")
    vid, sha, (first, second) = _video_with_scenes(lib)
    with _db(lib) as s:
        k = KINDS["redo_scene_vision"]
        [item] = candidates(s, k, [lib[2]], want=lineage.desired(s, k.artifact, _job_models(env[4])))
    assert item["asset_id"] == vid and item["redo"] is True  # the scheduler redescribes every scene
    _describe_scene(lib, first, sha)
    assert _due(lib, "redo_scene_vision") == [vid]  # half new, half old
    _describe_scene(lib, second, sha)
    assert _due(lib, "redo_scene_vision") == []
    assert _counts(lib, "scene_vision")["current"] == 1


# ---------------------------------------------------------------------------
# Stop and resume
# ---------------------------------------------------------------------------


def test_an_admin_stops_a_redo_and_resumes_it(env):
    from src.server.scheduler.service import _paused_in_database

    lib = _library(env, "RedoStop")
    client, headers, *_ = lib
    _stale_clip(lib, "a.jpg")
    try:
        assert client.post("/v1/producers/vision/redo/stop", headers=headers).status_code == 204
        p = _producer(lib)
        assert p["paused"] is True and p["paused_at"] and p["paused_by"]
        assert "vision" in _paused_in_database(env[4])
        assert p["counts"]["stale"] >= 1  # still stale, waiting
        assert client.post("/v1/producers/vision/redo/stop", headers=headers).status_code == 204  # again: fine
    finally:
        assert client.post("/v1/producers/vision/redo/resume", headers=headers).status_code == 204
    assert _producer(lib)["paused"] is False
    assert "vision" not in _paused_in_database(env[4])


def test_only_admins_stop_or_resume_and_only_they_see_who(env):
    from tests.test_archive_trash_safety import _key_with_role

    lib = _library(env, "RedoRoles")
    client, headers, *_ = lib
    editor = _key_with_role(env, "editor")
    viewer = _key_with_role(env, "viewer")
    assert client.post("/v1/producers/ocr/redo/stop", headers=editor).status_code == 403
    assert client.post("/v1/producers/ocr/redo/resume", headers=editor).status_code == 403
    try:
        assert client.post("/v1/producers/ocr/redo/stop", headers=headers).status_code == 204
        seen = _producer(lib, "ocr", headers=viewer)
        assert seen["paused"] is True and seen["paused_by"] is None
    finally:
        client.post("/v1/producers/ocr/redo/resume", headers=headers)


def test_what_isnt_redone_yet_cant_be_stopped_and_says_why(env):
    lib = _library(env, "RedoCant")
    client, headers, *_ = lib
    for artifact in ("scenes", "proxy", "video_preview"):
        p = _producer(lib, artifact)
        assert p["redoable"] is False and p["why_not"]
        r = client.post(f"/v1/producers/{artifact}/redo/stop", headers=headers)
        assert r.status_code == 409 and _error(r)["code"] == "cant_redo"
    assert _producer(lib, "vision")["redoable"] is True
    assert client.post("/v1/producers/nope/redo/stop", headers=headers).status_code == 404


def test_the_upgrade_step_is_gone(env):
    lib = _library(env, "RedoNoUpgrade")
    client, headers, *_ = lib
    assert client.post("/v1/producers/vision/upgrade", json={}, headers=headers).status_code in (404, 405)
    p = _producer(lib)
    assert "upgrades" not in p and "edited" not in p and "upgradable" not in p
    r = client.get("/v1/assets/page", params={"library_id": lib[2], "missing_vision": "true"}, headers=headers)
    assert r.status_code == 200 and all("upgrade" not in i for i in r.json()["items"])
    with _db(env) as s:
        tables = {r[0] for r in s.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
    assert "producer_redo_paused" in tables
    assert not tables & {"producer_upgrades", "producer_upgrade_items", "correction_history"}


# ---------------------------------------------------------------------------
# A new model asks once, naming what it makes again
# ---------------------------------------------------------------------------


def test_a_new_model_asks_with_the_count_then_redoes_and_resumes(env):
    from tests.test_ai_machines import BRAIN, _machines

    lib = _library(env, "RedoModel")
    client, headers, *_ = lib
    fake, _ = _machines({BRAIN: (VISION, "m1", "m2")})
    try:
        with fake:
            r = client.post("/v1/ai/machines", json={"name": "Brain", "api_url": BRAIN, "jobs": ["vision"],
                                                     "at_once": 2}, headers=headers)
            assert r.status_code == 201, r.text
            assert client.put("/v1/ai/jobs/vision", json={"model": "m1", "redo": True}, headers=headers).status_code == 200
            _stale_clip(lib, "a.jpg")
            assert client.post("/v1/producers/vision/redo/stop", headers=headers).status_code == 204

            r = client.put("/v1/ai/jobs/vision", json={"model": "m2"}, headers=headers)
            assert r.status_code == 409, r.text
            err = _error(r)
            assert err["code"] == "redo_on_change"
            assert err["details"]["clips"] >= 1 and err["details"]["model"] == "m2"
            kinds = {k["artifact"] for k in err["details"]["artifacts"]}
            assert "vision" in kinds
            assert "m2 makes" in err["message"] and "after anything missing" in err["message"]
            assert client.get("/v1/ai", headers=headers).json()["jobs"][0]["model"] == "m1"  # nothing changed

            # The same model again asks nothing.
            assert client.put("/v1/ai/jobs/vision", json={"model": "m1"}, headers=headers).status_code == 200
            assert _producer(lib)["paused"] is True  # not a change: still stopped

            r = client.put("/v1/ai/jobs/vision", json={"model": "m2", "redo": True}, headers=headers)
            assert r.status_code == 200, r.text
            assert _producer(lib)["paused"] is False  # the newest ask wins
    finally:
        client.post("/v1/producers/vision/redo/resume", headers=headers)
        for m in client.get("/v1/ai", headers=headers).json()["machines"]:
            if not m.get("built_in"):
                client.delete(f"/v1/ai/machines/{m['machine_id']}", headers=headers)
        _set_model(env, "vision", VISION)


def test_turning_a_job_off_or_on_with_the_same_settings_asks_nothing(env):
    lib = _library(env, "RedoOff")
    client, headers, *_ = lib
    vid = _ingest_with(lib, "t.mov", _sha(), None, media_type="video")
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30 WHERE asset_id = :a"), {"a": vid})
        s.commit()
    r = client.post(f"/v1/assets/{vid}/transcript", json={"srt": SRT, "source": "whisper",
                                                          "lineage": _want(lib, "transcript", None)}, headers=headers)
    assert r.status_code == 200, r.text
    try:
        assert client.put("/v1/ai/jobs/transcripts", json={"model": ""}, headers=headers).status_code == 200
        # small is the default: back on, no transcript's settings change.
        assert client.put("/v1/ai/jobs/transcripts", json={"model": "small"}, headers=headers).status_code == 200
        r = client.put("/v1/ai/jobs/transcripts", json={"model": "medium"}, headers=headers)
        assert r.status_code == 409 and _error(r)["code"] == "redo_on_change"
        assert any(k["artifact"] == "transcript" for k in _error(r)["details"]["artifacts"])
    finally:
        client.put("/v1/ai/jobs/transcripts", json={"model": "small", "redo": True}, headers=headers)


def test_a_new_model_doesnt_count_what_a_person_wrote(env):
    lib = _library(env, "RedoCountPerson")
    client, headers, *_ = lib
    with _db(lib) as s:
        before = lineage.made_by_a_producer(s, ["transcript"])["transcript"]
    vid = _ingest_with(lib, "t.mov", _sha(), None, media_type="video")
    with _db(lib) as s:
        s.execute(text("UPDATE assets SET duration_sec = 30 WHERE asset_id = :a"), {"a": vid})
        s.commit()
    r = client.post(f"/v1/assets/{vid}/transcript", json={"srt": SRT, "source": "manual"}, headers=headers)
    assert r.status_code == 200, r.text
    with _db(lib) as s:
        assert lineage.made_by_a_producer(s, ["transcript"])["transcript"] == before
