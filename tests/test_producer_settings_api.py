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
    faces = {f["key"]: f for f in _producer(env, "faces")["fields"]}
    assert all(f["fixed"] for f in faces.values())  # face detection doesn't read them yet


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
    r = _put(env, "faces", settings={"min_confidence": 0.7})
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
    client.post("/v1/producers/analysis_proxy/redo/resume", headers=headers)
