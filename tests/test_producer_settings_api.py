# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Producer settings an admin changes (ADR-016 phase 4).

Each producer declares its settings (src/producers/<artifact>/): GET
/v1/producers serves them with their bounds; PUT /v1/producers/{artifact}/
settings changes the ones its code reads, checked, and asks before
redoing what the old ones made, as a model change in Settings → AI does.
"""

from __future__ import annotations

import pytest

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_archive_by_hand import _sha
from tests.test_archive_trash_safety import _key_with_role
from tests.test_lineage_api import _ingest_with
from tests.test_reconciler import _counts, _describe, _library

pytestmark = pytest.mark.slow


def _producer(env, artifact: str) -> dict:
    client, headers, *_ = env
    r = client.get("/v1/producers", params={"counts": "false"}, headers=headers)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p for p in r.json()["producers"]}[artifact]


def _put(env, artifact: str, headers=None, **body):
    client, own, *_ = env
    return client.put(f"/v1/producers/{artifact}/settings", json=body, headers=headers or own)


@pytest.fixture(autouse=True)
def _defaults_after(env):
    yield
    for artifact, keys in {"transcript": ["vad_min_silence_ms"], "vision": ["prompt", "max_edge", "temperature"],
                           "analysis_proxy": ["max_edge", "crf"]}.items():
        _put(env, artifact, settings={k: None for k in keys}, redo=True)


def test_each_setting_comes_with_its_bounds_and_whether_it_can_change(env):
    fields = {f["key"]: f for f in _producer(env, "transcript")["fields"]}
    assert fields["vad_min_silence_ms"] == {
        "key": "vad_min_silence_ms", "label": "Shortest silence skipped", "kind": "int", "value": 500,
        "default": 500, "minimum": 100, "maximum": 2000, "unit": "ms", "advanced": False, "fixed": None}
    assert "Settings → AI" in fields["model"]["fixed"]
    clip = {f["key"]: f for f in _producer(env, "clip")["fields"]}
    assert all(f["fixed"] for f in clip.values())  # CLIP doesn't read them yet


def test_an_admin_changes_a_setting_and_the_hash_follows(env):
    before = _producer(env, "transcript")
    r = _put(env, "transcript", settings={"vad_min_silence_ms": 800})
    assert r.status_code == 200, r.text
    after = _producer(env, "transcript")
    assert after["settings"]["vad_min_silence_ms"] == 800
    assert after["settings_hash"] != before["settings_hash"]
    r = _put(env, "transcript", settings={"vad_min_silence_ms": None})  # back to its default
    assert r.status_code == 200 and _producer(env, "transcript")["settings_hash"] == before["settings_hash"]


@pytest.mark.parametrize("settings, code", [
    ({"vad_min_silence_ms": 50}, "bad_setting"),  # under its bounds
    ({"vad_min_silence_ms": "800"}, "bad_setting"),  # text for a number
    ({"vad_min_silence_ms": 1.5}, "bad_setting"),  # not a whole number
    ({"vad_min_silence_ms": True}, "bad_setting"),
    ({"silence": 800}, "unknown_setting"),
    ({"model": "large-v3"}, "setting_fixed"),  # its job's, in Settings → AI
])
def test_what_cant_be_set_is_refused_saying_why(env, settings, code):
    before = _producer(env, "transcript")["settings_hash"]
    r = _put(env, "transcript", settings=settings)
    assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text
    assert _producer(env, "transcript")["settings_hash"] == before


def test_a_setting_the_code_doesnt_read_cant_be_changed(env):
    r = _put(env, "clip", settings={"input_edge": 1024})
    assert r.status_code == 422 and r.json()["error"]["code"] == "setting_fixed", r.text
    assert "Not read by this producer yet" in r.json()["error"]["message"]


def test_new_settings_ask_before_making_again_what_the_old_ones_made(env):
    lib = _library(env, "SettingsRedo")
    sha = _sha()
    photo = _ingest_with(lib, "a.jpg", sha, None)
    _describe(lib, photo, sha)
    assert _counts(lib, "vision")["current"] == 1
    r = _put(env, "vision", settings={"temperature": 0.5})
    assert r.status_code == 409 and r.json()["error"]["code"] == "redo_on_change", r.text
    assert r.json()["error"]["details"]["clips"] >= 1
    assert _producer(env, "vision")["settings"]["temperature"] == 0.2  # nothing saved
    r = _put(env, "vision", settings={"temperature": 0.5}, redo=True)
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["temperature"] == 0.5
    assert _counts(lib, "vision")["stale"] == 1


def test_saving_settings_resumes_a_stopped_redo(env):
    client, headers, *_ = env
    assert client.post("/v1/producers/analysis_proxy/redo/stop", headers=headers).status_code == 204
    assert _producer(env, "analysis_proxy")["paused"] is True
    r = _put(env, "analysis_proxy", settings={"crf": 30}, redo=True)
    assert r.status_code == 200, r.text
    assert r.json()["paused"] is False and _producer(env, "analysis_proxy")["paused"] is False


def test_a_float_setting_takes_a_whole_number_as_the_same_value(env):
    r = _put(env, "vision", settings={"temperature": 1}, redo=True)
    assert r.status_code == 200 and r.json()["settings"]["temperature"] == 1.0
    assert isinstance(r.json()["settings"]["temperature"], float)


def test_only_admins_change_settings(env):
    for role in ("editor", "viewer"):
        r = _put(env, "transcript", headers=_key_with_role(env, role), settings={"vad_min_silence_ms": 800})
        assert r.status_code == 403, r.text
    assert _producer(env, "transcript")["settings"]["vad_min_silence_ms"] == 500


def test_an_unknown_producer_is_404(env):
    assert _put(env, "teleport", settings={}).status_code == 404


def test_saving_the_same_settings_leaves_a_stopped_redo_stopped(env):
    client, headers, *_ = env
    assert client.post("/v1/producers/analysis_proxy/redo/stop", headers=headers).status_code == 204
    r = _put(env, "analysis_proxy", settings={"crf": 28})  # its default: nothing changes
    assert r.status_code == 200 and r.json()["paused"] is True, r.text
    assert r.json()["paused_at"] and r.json()["paused_by"], r.text  # as GET says it, to an admin
    client.post("/v1/producers/analysis_proxy/redo/resume", headers=headers)


def _stored(env, artifact: str) -> str | None:
    """What system_metadata holds for the producer's changed settings."""
    from sqlalchemy import create_engine, text

    engine = create_engine(env[5])
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT value FROM system_metadata WHERE key = :k"),
                                {"k": f"producer.{artifact}"}).scalar()
    finally:
        engine.dispose()


def test_only_whats_changed_is_stored_and_back_to_defaults_stores_nothing(env):
    assert _put(env, "transcript", settings={"vad_min_silence_ms": 800}).status_code == 200
    assert _stored(env, "transcript") == '{"vad_min_silence_ms": 800}'
    assert _put(env, "transcript", settings={"vad_min_silence_ms": 500}).status_code == 200  # the default
    assert _stored(env, "transcript") is None


def test_the_count_asked_about_is_what_the_new_settings_would_redo(env):
    lib = _library(env, "SettingsCount")
    sha = _sha()
    photo = _ingest_with(lib, "a.jpg", sha, None)
    _describe(lib, photo, sha)
    assert _put(env, "vision", settings={"temperature": 0.5}, redo=True).status_code == 200
    assert _counts(lib, "vision")["stale"] == 1
    # Back before anything was made again: nothing would be, so nothing asks.
    r = _put(env, "vision", settings={"temperature": None})
    assert r.status_code == 200, r.text
    assert _counts(lib, "vision")["stale"] == 0


def test_the_question_says_a_stopped_redo_starts_again(env):
    client, headers, *_ = env
    lib = _library(env, "SettingsStopped")
    sha = _sha()
    _describe(lib, _ingest_with(lib, "a.jpg", sha, None), sha)
    assert client.post("/v1/producers/vision/redo/stop", headers=headers).status_code == 204
    try:
        r = _put(env, "vision", settings={"temperature": 0.7})
        assert r.status_code == 409, r.text
        assert "stopped" in r.json()["error"]["message"] and r.json()["error"]["details"]["paused"] is True
    finally:
        client.post("/v1/producers/vision/redo/resume", headers=headers)


@pytest.mark.parametrize("raw", [
    '{"settings": {"temperature": NaN}, "redo": true}',
    '{"settings": {"temperature": Infinity}, "redo": true}',
    '{"settings": {"max_tokens": Infinity}, "redo": true}',
    '{"settings": {"temperature": 1e400}, "redo": true}',
    '{"settings": {"max_tokens": 10000000000000000000000000000000000000000}, "redo": true}',
])
def test_numbers_json_allows_but_no_bounds_do_are_refused(env, raw):
    client, headers, *_ = env
    before = _producer(env, "vision")["settings_hash"]
    r = client.put("/v1/producers/vision/settings", content=raw,
                   headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_setting", r.text
    assert _producer(env, "vision")["settings_hash"] == before


@pytest.mark.parametrize("prompt, says", [("   ", "empty"), ("x" * 4001, "4,000")])
def test_a_prompt_must_say_something_and_not_too_much(env, prompt, says):
    r = _put(env, "vision", settings={"prompt": prompt}, redo=True)
    assert r.status_code == 422 and says in r.json()["error"]["message"], r.text


def test_a_new_prompt_is_saved_whole_with_its_equals_signs(env):
    prompt = "List objects as key=value pairs.\nThen a line: done=1"
    r = _put(env, "vision", settings={"prompt": prompt}, redo=True)
    assert r.status_code == 200, r.text
    assert _producer(env, "vision")["settings"]["prompt"] == prompt


def test_faces_settings_ask_saying_what_a_redo_keeps(env):
    from tests.test_lineage_api import _want

    lib = _library(env, "FaceSettings")
    client, headers, *_ = lib
    sha = _sha()
    photo = _ingest_with(lib, "a.jpg", sha, None)
    r = client.post(f"/v1/assets/{photo}/faces", json={
        "detection_model": "insightface", "detection_model_version": "buffalo_l", "faces": [],
        "lineage": _want(lib, "faces", sha)}, headers=headers)
    assert r.status_code == 201, r.text
    assert _counts(lib, "faces")["current"] == 1
    try:
        r = _put(env, "faces", settings={"min_confidence": 0.7})
        assert r.status_code == 409, r.text
        assert "Faces people named, or said aren't someone, are kept" in r.json()["error"]["message"]
        assert r.json()["error"]["details"]["clips"] >= 1
        r = _put(env, "faces", settings={"min_confidence": 0.7}, redo=True)
        assert r.status_code == 200 and r.json()["settings"]["min_confidence"] == 0.7, r.text
        assert _counts(lib, "faces")["stale"] == 1
    finally:
        _put(env, "faces", settings={"min_confidence": None}, redo=True)
    for settings in ({"model": "antelopev2"}, {"det_size": 320}):
        r = _put(env, "faces", settings=settings)
        assert r.status_code == 422 and r.json()["error"]["code"] == "setting_fixed", r.text
    r = _put(env, "faces", settings={"max_detect_edge": 2048})  # past the proxies' size
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_setting", r.text
