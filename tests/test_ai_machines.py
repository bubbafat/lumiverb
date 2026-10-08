# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The account's AI machines (ADR-016 phase 3): one list of GPU machines,
each with the jobs it does and how many requests it takes at once, and one
model per job (Robert's call, Oct 8).

An admin adds a machine by URL and optional key; Connect lists the models
it offers. A machine can do a job only if it offers that job's model, so
whatever machine made an artifact, it's the same model's (the machine is
the "where", out of lineage, like the encoder). The worker checks each
machine before sending it work and reports what it found per machine;
Settings shows it. Keys are never shown back, and a viewer never gets one.

Transcripts are the second job. Every account has a built-in machine: the
worker's own Whisper (no URL), doing transcripts one at a time, so they run
with nothing set up; transcription servers can share the work.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_archive_trash_safety import _key_with_role
from tests.test_lineage_api import _ingest_with, _sha

BRAIN = "http://172.18.0.6:11434/v1"
STUDIO = "http://10.10.10.1:11434/v1"
QWEN, LLAVA = "qwen3-vl:8b", "llava:13b"
# A transcription server (speaches) lists faster-whisper's models by repo.
SPEACHES = "http://10.10.10.2:8000/v1"
SMALL_REPO, LARGE_REPO = "Systran/faster-whisper-small", "Systran/faster-whisper-large-v3"
KOKORO = "speaches-ai/Kokoro-82M-v1.0-ONNX"
SRT = "1\n00:00:00,000 --> 00:00:01,000\nhello\n"


def _machines(answers: dict):
    """Fake OpenAI-compatible GET /models per machine URL: a tuple of model
    ids, an HTTP status, or an exception to raise. Unknown URLs refuse."""
    seen: list[dict] = []

    def get(url, headers=None, timeout=None, **_kwargs):
        seen.append({"url": url, "headers": headers or {}})
        answer = answers.get(url.removesuffix("/models"), requests.ConnectionError("refused"))
        if isinstance(answer, BaseException):
            raise answer
        resp = MagicMock(status_code=answer if isinstance(answer, int) else 200)
        resp.json.return_value = {"object": "list",
                                  "data": [{"id": m, "object": "model"} for m in (answer if isinstance(answer, tuple) else ())]}
        return resp

    return patch("src.shared.vision_endpoint.requests.get", side_effect=get), seen


def _ai(env, headers=None) -> dict:
    client, own, *_ = env
    r = client.get("/v1/ai", headers=headers or own)
    assert r.status_code == 200, r.text
    return r.json()


def _add(env, headers=None, **body):
    client, own, *_ = env
    body = {"name": "Brain", "api_url": BRAIN, "jobs": ["vision"], "at_once": 2, **body}
    return client.post("/v1/ai/machines", json=body, headers=headers or own)


def _model(env, job: str, model: str, headers=None):
    client, own, *_ = env
    return client.put(f"/v1/ai/jobs/{job}", json={"model": model}, headers=headers or own)


def _job(ai: dict, job: str) -> dict:
    return next(j for j in ai["jobs"] if j["job"] == job)


def _machine(ai: dict, name: str) -> dict:
    return next(m for m in ai["machines"] if m["name"] == name)


def _added(ai: dict) -> list[dict]:
    """The machines an admin added (all but the built-in one)."""
    return [m for m in ai["machines"] if not m["built_in"]]


def _built_in(ai: dict) -> dict:
    [m] = [m for m in ai["machines"] if m["built_in"]]
    return m


@pytest.fixture
def none(env):
    """Each test starts with no machines added and no vision model: only the
    built-in Whisper, as every account starts, doing transcripts with small."""
    from sqlalchemy import update

    from src.server.database import get_control_session
    from src.server.models.control_plane import AiMachine

    client, headers, *_ = env
    assert _model(env, "vision", "").status_code == 200
    for m in _ai(env)["machines"]:
        if m["built_in"]:
            r = client.patch(f"/v1/ai/machines/{m['machine_id']}", headers=headers, json={
                "name": "Built in", "jobs": ["transcripts"], "at_once": 1, "enabled": True})
        else:
            r = client.delete(f"/v1/ai/machines/{m['machine_id']}", params={"leave_jobs": True}, headers=headers)
        assert r.status_code == 200, r.text
    assert _model(env, "transcripts", "small").status_code == 200
    with get_control_session() as ctrl:  # not checked by a worker yet
        ctrl.execute(update(AiMachine).values(online=None, status_error="", models=[], checked_at=None))
        ctrl.commit()
    yield


@pytest.mark.slow
def test_connect_lists_what_a_machine_offers(env, none):
    client, headers, *_ = env
    fake, seen = _machines({BRAIN: (QWEN, LLAVA)})
    with fake:
        r = client.post("/v1/ai/connect", json={"api_url": BRAIN + "/", "api_key": "sk-1"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"models": [LLAVA, QWEN]}
    assert seen[0]["url"] == BRAIN + "/models" and seen[0]["headers"]["Authorization"] == "Bearer sk-1"


@pytest.mark.slow
@pytest.mark.parametrize(("answer", "says"), [
    (requests.ConnectionError("refused"), "Couldn't reach"),
    (requests.Timeout(), "didn't answer"),
    (401, "refused the key"),
    (404, "is the URL right"),
    ((), "lists no models"),
])
def test_connect_says_plainly_why_it_cant(env, none, answer, says):
    client, headers, *_ = env
    fake, _ = _machines({BRAIN: answer})
    with fake:
        r = client.post("/v1/ai/connect", json={"api_url": BRAIN}, headers=headers)
    assert r.status_code == 502, r.text
    error = r.json()["error"]
    assert error["code"] == "machine_unreachable" and says in error["message"]


@pytest.mark.slow
def test_machines_each_with_their_jobs_and_how_many_at_once(env, none):
    fake, _ = _machines({BRAIN: (QWEN,), STUDIO: (QWEN, LLAVA)})
    with fake:
        assert _add(env, name="Brain 3080", api_url=BRAIN + "/", at_once=2).status_code == 201
        r = _add(env, name="Mac Studio", api_url=STUDIO, api_key="sk-studio", at_once=4)
    assert r.status_code == 201, r.text
    assert "sk-studio" not in r.text  # never shown back
    ai = r.json()
    # The built-in first, then in the order added.
    assert [m["name"] for m in ai["machines"]] == ["Built in", "Brain 3080", "Mac Studio"]
    _, brain, studio = ai["machines"]
    assert (brain["api_url"], brain["has_key"], brain["jobs"], brain["at_once"], brain["enabled"]) == (
        BRAIN, False, ["vision"], 2, True)
    assert (studio["has_key"], studio["at_once"]) == (True, 4)
    # What Connect found is what's known of it until the worker checks.
    assert studio["status"]["online"] is True and studio["status"]["models"] == [LLAVA, QWEN]
    assert _job(ai, "vision") == {"job": "vision", "label": "Descriptions & text", "model": "",
                                  "machines": 2, "offering": 0, "choices": [LLAVA, QWEN]}
    assert not brain["built_in"] and not studio["built_in"]


@pytest.mark.slow
@pytest.mark.parametrize("body", [
    {"at_once": 0}, {"at_once": 33}, {"jobs": ["audio"]}, {"name": ""}, {"api_url": ""},
])
def test_a_machine_needs_a_name_a_url_known_jobs_and_a_sane_limit(env, none, body):
    fake, _ = _machines({BRAIN: (QWEN,)})
    with fake:
        assert _add(env, **body).status_code == 422


@pytest.mark.slow
def test_names_are_unique(env, none):
    fake, _ = _machines({BRAIN: (QWEN,), STUDIO: (QWEN,)})
    with fake:
        assert _add(env, name="GPU").status_code == 201
        r = _add(env, name="GPU", api_url=STUDIO)
    assert r.status_code == 409 and r.json()["error"]["code"] == "name_taken"


@pytest.mark.slow
def test_a_machine_doing_a_job_must_offer_its_model(env, none):
    fake, _ = _machines({BRAIN: (QWEN,), STUDIO: (LLAVA,)})
    with fake:
        _add(env, name="Brain")
        assert _model(env, "vision", QWEN).status_code == 200
        r = _add(env, name="Studio", api_url=STUDIO)
        assert r.status_code == 409, r.text
        error = r.json()["error"]
        assert error["code"] == "model_not_offered"
        assert error["details"] == {"job": "vision", "model": QWEN, "models": [LLAVA]}
        # It can join without the job.
        assert _add(env, name="Studio", api_url=STUDIO, jobs=[]).status_code == 201
        studio = _machine(_ai(env), "Studio")["machine_id"]
        client, headers, *_ = env
        r = client.patch(f"/v1/ai/machines/{studio}", json={"jobs": ["vision"]}, headers=headers)
        assert r.status_code == 409 and r.json()["error"]["code"] == "model_not_offered"


@pytest.mark.slow
def test_a_jobs_model_must_be_offered_by_a_machine_doing_it(env, none):
    fake, _ = _machines({BRAIN: (QWEN, LLAVA), STUDIO: (QWEN,)})
    with fake:
        _add(env, name="Brain")
        _add(env, name="Studio", api_url=STUDIO)
    # The studio has gone offline since.
    fake, _ = _machines({BRAIN: (QWEN, LLAVA)})
    with fake:
        r = _model(env, "vision", "gpt-9")
        assert r.status_code == 409, r.text
        error = r.json()["error"]
        assert error["code"] == "model_not_offered" and error["details"]["model"] == "gpt-9"
        assert {m["name"]: m["models"] for m in error["details"]["machines"]} == {"Brain": [LLAVA, QWEN], "Studio": []}
        assert "Couldn't reach" in next(m for m in error["details"]["machines"] if m["name"] == "Studio")["error"]

        r = _model(env, "vision", QWEN)
    assert r.status_code == 200, r.text
    ai = r.json()
    assert _job(ai, "vision") == {"job": "vision", "label": "Descriptions & text", "model": QWEN,
                                  "machines": 2, "offering": 1, "choices": [LLAVA, QWEN]}
    # What saving found is each machine's status until the worker checks.
    assert _machine(ai, "Brain")["status"]["online"] is True
    studio = _machine(ai, "Studio")["status"]
    assert studio["online"] is False and "Couldn't reach" in studio["error"]


@pytest.mark.slow
def test_unknown_jobs_are_404(env, none):
    assert _model(env, "audio", "whisper-1").status_code == 404


@pytest.mark.slow
def test_the_saved_key_is_kept_and_used_until_replaced(env, none):
    client, headers, *_ = env
    fake, seen = _machines({BRAIN: (QWEN,), STUDIO: (QWEN,)})
    with fake:
        _add(env, api_key="sk-1")
        brain = _machine(_ai(env), "Brain")["machine_id"]
        # Connect for a saved machine uses its key when none is given.
        assert client.post("/v1/ai/connect", json={"api_url": BRAIN, "machine_id": brain},
                           headers=headers).status_code == 200
        assert seen[-1]["headers"]["Authorization"] == "Bearer sk-1"
        # Another URL never gets it.
        client.post("/v1/ai/connect", json={"api_url": STUDIO, "machine_id": brain}, headers=headers)
        assert "Authorization" not in seen[-1]["headers"]
        r = client.patch(f"/v1/ai/machines/{brain}", json={"name": "Brain 3080"}, headers=headers)
        assert r.status_code == 200 and _machine(r.json(), "Brain 3080")["has_key"] is True
        r = client.patch(f"/v1/ai/machines/{brain}", json={"api_key": ""}, headers=headers)
        assert _machine(r.json(), "Brain 3080")["has_key"] is False


@pytest.mark.slow
def test_the_worker_gets_a_jobs_machines_with_their_keys_and_a_viewer_doesnt(env, none):
    client, headers, *_ = env
    fake, _ = _machines({BRAIN: (QWEN,), STUDIO: (QWEN,)})
    with fake:
        _add(env, name="Brain", api_key="sk-1", at_once=2)
        _add(env, name="Studio", api_url=STUDIO, at_once=4)
        _model(env, "vision", QWEN)
    studio = _machine(_ai(env), "Studio")["machine_id"]
    worker = _key_with_role(env, "editor")
    r = client.get("/v1/ai/jobs/vision", headers=worker)
    assert r.status_code == 200, r.text
    assert r.json() == {"job": "vision", "model": QWEN, "machines": [
        {"machine_id": _machine(_ai(env), "Brain")["machine_id"], "name": "Brain", "api_url": BRAIN,
         "api_key": "sk-1", "at_once": 2, "built_in": False},
        {"machine_id": studio, "name": "Studio", "api_url": STUDIO, "api_key": "", "at_once": 4, "built_in": False},
    ]}
    assert client.get("/v1/ai/jobs/vision", headers=_key_with_role(env, "viewer")).status_code == 403

    # A machine turned off gets no work.
    assert client.patch(f"/v1/ai/machines/{studio}", json={"enabled": False}, headers=headers).status_code == 200
    assert [m["name"] for m in client.get("/v1/ai/jobs/vision", headers=worker).json()["machines"]] == ["Brain"]
    assert _job(_ai(env), "vision")["machines"] == 1

    # Clients that know one endpoint (the Mac app) get the first vision machine's.
    editor = client.get("/v1/tenant/context", headers=worker).json()
    assert (editor["vision_api_url"], editor["vision_api_key"], editor["vision_model_id"]) == (BRAIN, "sk-1", QWEN)
    viewer = client.get("/v1/tenant/context", headers=_key_with_role(env, "viewer")).json()
    assert viewer["vision_api_key"] == "" and viewer["vision_model_id"] == QWEN


@pytest.mark.slow
def test_only_admins_change_machines_or_models(env, none):
    client, headers, *_ = env
    editor = _key_with_role(env, "editor")
    fake, _ = _machines({BRAIN: (QWEN,)})
    with fake:
        assert _add(env).status_code == 201
        brain = _machine(_ai(env), "Brain")["machine_id"]
        assert client.post("/v1/ai/connect", json={"api_url": BRAIN}, headers=editor).status_code == 403
        assert _add(env, name="Other", headers=editor).status_code == 403
        assert client.patch(f"/v1/ai/machines/{brain}", json={"at_once": 3}, headers=editor).status_code == 403
        assert client.delete(f"/v1/ai/machines/{brain}", headers=editor).status_code == 403
        assert _model(env, "vision", QWEN, headers=editor).status_code == 403
    viewer = _machine(_ai(env, headers=_key_with_role(env, "viewer")), "Brain")
    assert viewer["has_key"] is False and "api_key" not in viewer


@pytest.mark.slow
def test_the_workers_check_shows_per_machine_until_fixed(env, none):
    client, headers, *_ = env
    fake, _ = _machines({BRAIN: (QWEN,), STUDIO: (QWEN,)})
    with fake:
        _add(env, name="Brain")
        _add(env, name="Studio", api_url=STUDIO)
        _model(env, "vision", QWEN)
    studio = _machine(_ai(env), "Studio")["machine_id"]
    worker = _key_with_role(env, "editor")
    r = client.post(f"/v1/ai/machines/{studio}/status",
                    json={"online": False, "error": f"Couldn't reach {STUDIO}: ConnectionError.", "models": []},
                    headers=worker)
    assert r.status_code == 204, r.text
    status = _machine(_ai(env), "Studio")["status"]
    assert status["online"] is False and "Couldn't reach" in status["error"] and status["checked_at"]
    assert _job(_ai(env), "vision")["offering"] == 1
    assert client.post(f"/v1/ai/machines/{studio}/status", json={"online": True, "models": [QWEN]},
                       headers=_key_with_role(env, "viewer")).status_code == 403
    assert client.post("/v1/ai/machines/aim_nope/status", json={"online": True, "models": []},
                       headers=worker).status_code == 404

    client.post(f"/v1/ai/machines/{studio}/status", json={"online": True, "models": [QWEN]}, headers=worker)
    assert _machine(_ai(env), "Studio")["status"]["online"] is True
    assert _job(_ai(env), "vision")["offering"] == 2

    # A machine moved to another URL: what was known of the old one goes, and Connect's answer stands.
    with fake:
        r = client.patch(f"/v1/ai/machines/{studio}", json={"api_url": BRAIN + "/"}, headers=headers)
    assert r.status_code == 200, r.text
    assert _machine(r.json(), "Studio")["status"]["models"] == [QWEN]


@pytest.mark.slow
def test_leaving_a_job_without_a_machine_needs_saying_so(env, none):
    client, headers, *_ = env
    fake, _ = _machines({BRAIN: (QWEN,), STUDIO: (QWEN,)})
    with fake:
        _add(env, name="Brain")
        _add(env, name="Studio", api_url=STUDIO)
        _model(env, "vision", QWEN)
    brain, studio = (m["machine_id"] for m in _added(_ai(env)))
    # One of two: no question.
    assert client.patch(f"/v1/ai/machines/{studio}", json={"enabled": False}, headers=headers).status_code == 200
    # The last one doing descriptions: the API asks, whichever way it would go.
    for method, path, body in (("patch", f"/v1/ai/machines/{brain}", {"enabled": False}),
                               ("patch", f"/v1/ai/machines/{brain}", {"jobs": []}),
                               ("delete", f"/v1/ai/machines/{brain}", None)):
        r = client.request(method.upper(), path, json=body, headers=headers)
        assert r.status_code == 409, (method, body, r.text)
        error = r.json()["error"]
        assert error["code"] == "job_left_without_machine"
        assert error["details"] == {"jobs": [{"job": "vision", "label": "Descriptions & text", "model": QWEN}]}
    assert _job(_ai(env), "vision")["machines"] == 1
    r = client.request("DELETE", f"/v1/ai/machines/{brain}", params={"leave_jobs": True}, headers=headers)
    assert r.status_code == 200 and _job(r.json(), "vision")["machines"] == 0
    # With the job off, nothing to ask.
    with fake:
        _model(env, "vision", "")
    assert client.delete(f"/v1/ai/machines/{studio}", headers=headers).status_code == 200


@pytest.mark.slow
def test_removing_a_machine(env, none):
    client, headers, *_ = env
    fake, _ = _machines({BRAIN: (QWEN,)})
    with fake:
        _add(env)
    brain = _machine(_ai(env), "Brain")["machine_id"]
    r = client.delete(f"/v1/ai/machines/{brain}", headers=headers)
    assert r.status_code == 200 and _added(r.json()) == []
    assert client.delete(f"/v1/ai/machines/{brain}", headers=headers).status_code == 404


@pytest.mark.slow
def test_no_model_turns_the_job_off(env, none):
    client, *_ = env
    fake, _ = _machines({BRAIN: (QWEN,)})
    with fake:
        _add(env)
        _model(env, "vision", QWEN)
        r = _model(env, "vision", "")
    assert r.status_code == 200 and _job(r.json(), "vision")["model"] == ""
    assert client.get("/v1/ai/jobs/vision", headers=_key_with_role(env, "editor")).json()["model"] == ""


@pytest.mark.slow
def test_changing_the_model_makes_descriptions_stale(env, none):
    from tests.test_lineage_api import _want
    from tests.test_reconciler import _counts, _describe, _library

    lib = _library(env, "AiStale")
    fake, _ = _machines({BRAIN: (QWEN, LLAVA)})
    with fake:
        _add(env)
        _model(env, "vision", QWEN)
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    assert _want(env, "vision", sha)["settings_hash"]
    _describe(lib, clip, sha)
    assert _counts(lib, "vision")["current"] == 1
    with fake:
        _model(env, "vision", LLAVA)
    assert _counts(lib, "vision")["stale"] == 1


@pytest.mark.slow
def test_a_machine_moved_to_another_url_leaves_its_key_behind(env, none):
    client, headers, *_ = env
    fake, seen = _machines({BRAIN: (QWEN,), STUDIO: (QWEN,)})
    with fake:
        _add(env, name="Box", api_key="sk-brain")
        box = _machine(_ai(env), "Box")["machine_id"]
        r = client.patch(f"/v1/ai/machines/{box}", json={"api_url": STUDIO}, headers=headers)
    assert r.status_code == 200, r.text
    assert "Authorization" not in seen[-1]["headers"]  # never sent to the new host
    assert _machine(r.json(), "Box")["has_key"] is False


@pytest.mark.slow
def test_a_machine_that_cant_answer_can_still_be_renamed_or_given_another_limit(env, none):
    """The web form sends every field; only real changes to where it is, its
    key, the jobs it's given, or turning it on, ask it again."""
    client, headers, *_ = env
    fake, _ = _machines({STUDIO: (QWEN,)})
    with fake:
        _add(env, name="Studio", api_url=STUDIO, at_once=4)
        _model(env, "vision", QWEN)
    studio = _machine(_ai(env), "Studio")["machine_id"]
    asleep, _ = _machines({})
    with asleep:
        r = client.patch(f"/v1/ai/machines/{studio}", json={
            "name": "Mac Studio", "api_url": STUDIO + "/", "jobs": ["vision"], "at_once": 2, "enabled": True},
            headers=headers)
        assert r.status_code == 200, r.text
        assert (_machine(r.json(), "Mac Studio")["at_once"]) == 2
        # Giving it a job it hasn't got, or turning it back on, needs it to answer.
        client.patch(f"/v1/ai/machines/{studio}?leave_jobs=true", json={"enabled": False}, headers=headers)
        r = client.patch(f"/v1/ai/machines/{studio}", json={"enabled": True}, headers=headers)
        assert r.status_code == 502 and r.json()["error"]["code"] == "machine_unreachable"


@pytest.mark.slow
def test_a_tenant_made_with_a_vision_url_gets_its_first_machine(env, none):
    from src.server.api.routers.ai import first_vision_machine
    from src.server.database import get_control_session

    client, *_ = env
    with patch("src.server.api.routers.admin.provision_tenant_database"):
        r = client.post("/v1/admin/tenants", json={"name": "WithVision", "vision_api_url": BRAIN + "/",
                                                   "vision_api_key": "sk-v"},
                        headers={"Authorization": "Bearer test-admin-analysis"})
    assert r.status_code == 200, r.text
    with get_control_session() as ctrl:
        machine = first_vision_machine(ctrl, r.json()["tenant_id"])
        assert (machine.api_url, machine.api_key, machine.jobs, machine.name) == (BRAIN, "sk-v", ["vision"], "172.18.0.6:11434")


@pytest.mark.slow
def test_the_admin_vision_url_never_clashes_with_a_machines_name(env, none):
    client, headers, _lib, _storage, tenant_id, _url = env
    fake, _ = _machines({BRAIN: (QWEN,)})
    with fake:
        assert _add(env, name="172.18.0.6:11434", jobs=[]).status_code == 201  # not doing vision
    r = client.patch(f"/v1/admin/tenants/{tenant_id}", json={"vision_api_url": BRAIN},
                     headers={"Authorization": "Bearer test-admin-analysis"})
    assert r.status_code == 200, r.text
    assert sorted(m["name"] for m in _added(_ai(env))) == ["172.18.0.6:11434", "172.18.0.6:11434 (2)"]


# ---------------------------------------------------------------------------
# Transcripts: the built-in Whisper, and transcription servers
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_every_account_starts_with_the_built_in_whisper_doing_transcripts(env, none):
    from src.shared.whisper_models import BUILT_IN_MODELS

    ai = _ai(env)
    built_in = _built_in(ai)
    assert ai["machines"][0] == built_in
    assert {k: built_in[k] for k in ("name", "api_url", "has_key", "jobs", "at_once", "enabled", "status")} == {
        "name": "Built in", "api_url": "", "has_key": False, "jobs": ["transcripts"], "at_once": 1,
        "enabled": True, "status": None}
    assert _job(ai, "transcripts") == {"job": "transcripts", "label": "Transcripts", "model": "small",
                                       "machines": 1, "offering": 0, "choices": BUILT_IN_MODELS}


@pytest.mark.slow
def test_a_new_account_gets_its_built_in_whisper(env, none):
    from src.server.database import get_control_session
    from src.server.models.control_plane import Tenant
    from src.server.repository.ai_machines import machines

    client, *_ = env
    with patch("src.server.api.routers.admin.provision_tenant_database"):
        r = client.post("/v1/admin/tenants", json={"name": "FreshWhisper"},
                        headers={"Authorization": "Bearer test-admin-analysis"})
    assert r.status_code == 200, r.text
    tenant_id = r.json()["tenant_id"]
    with get_control_session() as ctrl:
        [m] = machines(ctrl, tenant_id)
        assert (m.built_in, m.name, m.api_url, m.api_key, m.jobs, m.at_once, m.enabled) == (
            True, "Built in", "", "", ["transcripts"], 1, True)
        assert ctrl.get(Tenant, tenant_id).transcript_model_id == "small"


@pytest.mark.slow
def test_the_built_in_whisper_stays_and_only_transcribes(env, none):
    client, headers, *_ = env
    built_in = _built_in(_ai(env))["machine_id"]
    r = client.delete(f"/v1/ai/machines/{built_in}", params={"leave_jobs": True}, headers=headers)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "built_in_machine" and "turn it off" in r.json()["error"]["message"]
    for body in ({"api_url": SPEACHES}, {"api_key": "sk-1"}, {"jobs": ["vision", "transcripts"]}):
        r = client.patch(f"/v1/ai/machines/{built_in}", json=body, headers=headers)
        assert r.status_code == 409, (body, r.text)
        assert r.json()["error"]["code"] == "built_in_machine"
    assert _built_in(_ai(env))["jobs"] == ["transcripts"]
    # Adding one takes a URL: there's only the one built in.
    assert _add(env, name="Another", api_url="", jobs=["transcripts"]).status_code == 422


@pytest.mark.slow
def test_the_built_in_whisper_can_be_renamed_given_more_at_once_or_turned_off(env, none):
    client, headers, *_ = env
    built_in = _built_in(_ai(env))["machine_id"]
    fake, seen = _machines({})
    with fake:
        r = client.patch(f"/v1/ai/machines/{built_in}", json={"name": "Brain Whisper", "at_once": 2},
                         headers=headers)
        assert r.status_code == 200, r.text
        # Turning off the only machine doing transcripts asks first.
        r = client.patch(f"/v1/ai/machines/{built_in}", json={"enabled": False}, headers=headers)
        assert r.status_code == 409, r.text
        assert r.json()["error"]["details"] == {
            "jobs": [{"job": "transcripts", "label": "Transcripts", "model": "small"}]}
        r = client.patch(f"/v1/ai/machines/{built_in}", params={"leave_jobs": True}, json={"enabled": False},
                         headers=headers)
        assert r.status_code == 200, r.text
        assert client.get("/v1/ai/jobs/transcripts", headers=_key_with_role(env, "editor")).json()["machines"] == []
        # Back on: nothing to ask (it has no URL); the worker checks it.
        r = client.patch(f"/v1/ai/machines/{built_in}", json={"enabled": True}, headers=headers)
        assert r.status_code == 200, r.text
    assert seen == []
    m = _built_in(r.json())
    assert (m["name"], m["at_once"], m["enabled"], m["status"]) == ("Brain Whisper", 2, True, None)


@pytest.mark.slow
def test_a_transcription_server_offers_whisper_by_its_repo_name(env, none):
    client, headers, *_ = env
    fake, _ = _machines({SPEACHES: (SMALL_REPO, KOKORO), BRAIN: (QWEN,)})
    with fake:
        r = client.post("/v1/ai/connect", json={"api_url": SPEACHES}, headers=headers)
        assert r.status_code == 200 and r.json() == {"models": ["small", KOKORO]}
        r = _add(env, name="Speaches", api_url=SPEACHES, jobs=["transcripts"], at_once=2)
        assert r.status_code == 201, r.text
        # An Ollama box doesn't do Whisper.
        r = _add(env, name="Brain", jobs=["vision", "transcripts"])
        assert r.status_code == 409, r.text
        assert r.json()["error"]["details"] == {"job": "transcripts", "model": "small", "models": [QWEN]}
    ai = _ai(env)
    assert _machine(ai, "Speaches")["status"]["models"] == ["small", KOKORO]
    # The built-in hasn't been checked by a worker yet.
    assert (_job(ai, "transcripts")["machines"], _job(ai, "transcripts")["offering"]) == (2, 1)


@pytest.mark.slow
def test_the_transcripts_model_is_one_a_machine_doing_them_offers(env, none):
    from src.shared.whisper_models import BUILT_IN_MODELS

    client, headers, *_ = env
    fake, _ = _machines({SPEACHES: (SMALL_REPO, LARGE_REPO, "deepdml/faster-whisper-large-v3-turbo-ct2")})
    with fake:
        _add(env, name="Speaches", api_url=SPEACHES, jobs=["transcripts"])
        # Either name: kept by its one name.
        r = _model(env, "transcripts", LARGE_REPO)
        assert r.status_code == 200, r.text
        assert _job(r.json(), "transcripts")["model"] == "large-v3"
        assert "deepdml/faster-whisper-large-v3-turbo-ct2" in _job(r.json(), "transcripts")["choices"]
        # No machine doing transcripts offers OpenAI's.
        r = _model(env, "transcripts", "whisper-1")
        assert r.status_code == 409, r.text
        error = r.json()["error"]
        assert error["code"] == "model_not_offered"
        asked = {m["name"]: m for m in error["details"]["machines"]}
        assert asked["Built in"]["models"] == BUILT_IN_MODELS and asked["Built in"]["error"] == ""
        assert asked["Speaches"]["models"] == ["deepdml/faster-whisper-large-v3-turbo-ct2", "large-v3", "small"]
    # Saving asks the servers; the built-in is the worker's to check.
    assert _built_in(_ai(env))["status"] is None
    worker = client.get("/v1/ai/jobs/transcripts", headers=_key_with_role(env, "editor")).json()
    assert worker["model"] == "large-v3"
    assert [(m["name"], m["api_url"], m["at_once"], m["built_in"]) for m in worker["machines"]] == [
        ("Built in", "", 1, True), ("Speaches", SPEACHES, 2, False)]


@pytest.mark.slow
def test_the_workers_check_of_the_built_in_whisper_shows(env, none):
    from src.shared.whisper_models import BUILT_IN_MODELS

    client, *_ = env
    built_in = _built_in(_ai(env))["machine_id"]
    worker = _key_with_role(env, "editor")
    r = client.post(f"/v1/ai/machines/{built_in}/status", json={"online": True, "models": BUILT_IN_MODELS},
                    headers=worker)
    assert r.status_code == 204, r.text
    assert _job(_ai(env), "transcripts")["offering"] == 1
    client.post(f"/v1/ai/machines/{built_in}/status",
                json={"online": False, "error": "faster-whisper isn't installed on brain."}, headers=worker)
    status = _built_in(_ai(env))["status"]
    assert status["online"] is False and "faster-whisper" in status["error"]
    assert _job(_ai(env), "transcripts")["offering"] == 0


@pytest.mark.slow
def test_changing_the_transcripts_model_makes_transcripts_stale(env, none):
    from tests.test_lineage_api import _want
    from tests.test_reconciler import _counts, _library

    lib = _library(env, "TranscriptsStale")
    client, headers, *_ = lib
    sha = _sha()
    clip = _ingest_with(lib, "talk.mov", sha, None, media_type="video")
    client.put(f"/v1/assets/{clip}/video-facet", json={"duration_sec": 5.0, "lineage": _want(lib, "probe", sha)},
               headers=headers)
    r = client.post(f"/v1/assets/{clip}/transcript", json={
        "srt": SRT, "source": "whisper", "lineage": _want(lib, "transcript", sha)}, headers=headers)
    assert r.status_code == 200, r.text
    assert _counts(lib, "transcript")["current"] == 1
    assert _model(env, "transcripts", "medium").status_code == 200
    assert _counts(lib, "transcript")["stale"] == 1
    r = client.get("/v1/producers", params={"counts": "false"}, headers=headers)
    assert {p["artifact"]: p for p in r.json()["producers"]}["transcript"]["settings"] == {
        "model": "medium", "vad_min_silence_ms": 500}
    # The vision model is the vision job's alone.
    fake, _ = _machines({BRAIN: (QWEN,)})
    with fake:
        _add(env)
        _model(env, "vision", QWEN)
    r = client.get("/v1/producers", params={"counts": "false"}, headers=headers)
    settings = {p["artifact"]: p["settings"] for p in r.json()["producers"]}
    assert settings["transcript"]["model"] == "medium" and settings["vision"]["model"] == QWEN
