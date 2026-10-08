# ruff: noqa: F811 — pytest fixtures are named as parameters
"""The account's vision AI lives in one place (ADR-016 phase 3).

An admin enters the endpoint URL and an optional key and presses Connect,
which lists the models the endpoint offers; they pick one and save, and
the endpoint is asked again so only an offered model is saved. The key is
never shown back. The worker checks the model before vision work and
reports when it can't use it; Settings shows that until it's fixed.
Changing the model makes descriptions stale.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_archive_trash_safety import _key_with_role
from tests.test_lineage_api import _ingest_with, _sha

URL = "http://vision.local:11434/v1"


def _endpoint(models=("qwen3-vl:8b", "llava:13b"), status=200, raises=None):
    """A fake OpenAI-compatible GET /models."""
    seen: list[dict] = []

    def get(url, headers=None, timeout=None):
        seen.append({"url": url, "headers": headers or {}})
        if raises:
            raise raises
        resp = MagicMock(status_code=status)
        resp.json.return_value = {"object": "list", "data": [{"id": m, "object": "model"} for m in models]}
        return resp

    return patch("src.shared.vision_endpoint.requests.get", side_effect=get), seen


def _get(env, headers=None):
    client, own, *_ = env
    r = client.get("/v1/tenant/vision", headers=headers or own)
    assert r.status_code == 200, r.text
    return r.json()


def _save(env, **body):
    client, headers, *_ = env
    return client.put("/v1/tenant/vision", json=body, headers=headers)


@pytest.fixture
def off(env):
    """Each test starts with vision AI off."""
    fake, _ = _endpoint()
    with fake:
        assert _save(env, api_url="").status_code == 200
    yield


@pytest.mark.slow
def test_connect_lists_what_the_endpoint_offers(env, off):
    client, headers, *_ = env
    fake, seen = _endpoint()
    with fake:
        r = client.post("/v1/tenant/vision/connect", json={"api_url": URL + "/", "api_key": "sk-1"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"models": ["llava:13b", "qwen3-vl:8b"]}
    assert seen[0]["url"] == URL + "/models" and seen[0]["headers"]["Authorization"] == "Bearer sk-1"


@pytest.mark.slow
@pytest.mark.parametrize(("kwargs", "says"), [
    ({"raises": requests.ConnectionError("refused")}, "Couldn't reach"),
    ({"raises": requests.Timeout()}, "didn't answer"),
    ({"status": 401}, "refused the key"),
    ({"status": 404}, "is the URL right"),
    ({"models": ()}, "lists no models"),
])
def test_connect_says_plainly_why_it_cant(env, off, kwargs, says):
    client, headers, *_ = env
    fake, _ = _endpoint(**kwargs)
    with fake:
        r = client.post("/v1/tenant/vision/connect", json={"api_url": URL}, headers=headers)
    assert r.status_code == 502, r.text
    error = r.json()["error"]
    assert error["code"] == "vision_unreachable" and says in error["message"]


@pytest.mark.slow
def test_only_a_model_the_endpoint_offers_is_saved(env, off):
    fake, _ = _endpoint()
    with fake:
        r = _save(env, api_url=URL, api_key="sk-1", model="gpt-9")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "vision_model_unavailable"
    assert r.json()["error"]["details"]["models"] == ["llava:13b", "qwen3-vl:8b"]
    assert _get(env)["api_url"] == ""  # nothing saved

    with fake:
        r = _save(env, api_url=URL, api_key="sk-1", model="qwen3-vl:8b")
    assert r.status_code == 200, r.text
    saved = r.json()
    assert (saved["api_url"], saved["has_key"], saved["model"]) == (URL, True, "qwen3-vl:8b")
    assert saved["status"]["ok"] is True
    assert "sk-1" not in r.text  # the key is never shown back


@pytest.mark.slow
def test_the_saved_key_is_kept_and_used_until_replaced(env, off):
    client, headers, *_ = env
    fake, seen = _endpoint()
    with fake:
        assert _save(env, api_url=URL, api_key="sk-1", model="qwen3-vl:8b").status_code == 200
        r = client.post("/v1/tenant/vision/connect", json={"api_url": URL}, headers=headers)
        assert r.status_code == 200
        assert seen[-1]["headers"]["Authorization"] == "Bearer sk-1"
        assert _save(env, api_url=URL, model="llava:13b").json()["has_key"] is True
        # Another endpoint doesn't get this one's key.
        client.post("/v1/tenant/vision/connect", json={"api_url": "http://other:1234/v1"}, headers=headers)
        assert "Authorization" not in seen[-1]["headers"]
        assert _save(env, api_url=URL, api_key="", model="llava:13b").json()["has_key"] is False


@pytest.mark.slow
def test_the_worker_gets_the_key_and_a_viewer_doesnt(env, off):
    client, headers, *_ = env
    fake, _ = _endpoint()
    with fake:
        _save(env, api_url=URL, api_key="sk-1", model="qwen3-vl:8b")
    editor = client.get("/v1/tenant/context", headers=_key_with_role(env, "editor")).json()
    assert (editor["vision_api_url"], editor["vision_api_key"], editor["vision_model_id"]) == (URL, "sk-1", "qwen3-vl:8b")
    viewer = client.get("/v1/tenant/context", headers=_key_with_role(env, "viewer")).json()
    assert viewer["vision_api_key"] == "" and viewer["vision_model_id"] == "qwen3-vl:8b"


@pytest.mark.slow
def test_only_admins_connect_or_save(env, off):
    client, *_ = env
    editor = _key_with_role(env, "editor")
    fake, _ = _endpoint()
    with fake:
        assert client.post("/v1/tenant/vision/connect", json={"api_url": URL}, headers=editor).status_code == 403
        assert client.put("/v1/tenant/vision", json={"api_url": URL, "model": "llava:13b"},
                          headers=editor).status_code == 403
    assert _get(env, headers=_key_with_role(env, "viewer"))["api_url"] == ""


@pytest.mark.slow
def test_the_workers_report_shows_until_fixed(env, off):
    client, headers, *_ = env
    fake, _ = _endpoint()
    with fake:
        _save(env, api_url=URL, model="qwen3-vl:8b")
    worker = _key_with_role(env, "editor")
    r = client.post("/v1/tenant/vision/status", json={
        "ok": False, "error": f"{URL} no longer offers qwen3-vl:8b.", "model": "qwen3-vl:8b", "api_url": URL},
        headers=worker)
    assert r.status_code == 200, r.text
    status = _get(env)["status"]
    assert status["ok"] is False and "no longer offers" in status["error"] and status["checked_at"]
    assert client.post("/v1/tenant/vision/status", json={"ok": True},
                       headers=_key_with_role(env, "viewer")).status_code == 403

    # Saving a model the endpoint offers fixes it.
    with fake:
        _save(env, api_url=URL, model="llava:13b")
    assert _get(env)["status"]["ok"] is True
    # A report about other settings than these says nothing about them.
    client.post("/v1/tenant/vision/status", json={"ok": False, "error": "x", "model": "qwen3-vl:8b",
                                                  "api_url": URL}, headers=worker)
    assert _get(env)["status"] is None


@pytest.mark.slow
def test_an_empty_url_turns_vision_off(env, off):
    fake, _ = _endpoint()
    with fake:
        _save(env, api_url=URL, api_key="sk-1", model="qwen3-vl:8b")
        r = _save(env, api_url="")
    assert r.status_code == 200
    assert r.json() == {"api_url": "", "has_key": False, "model": "", "status": None}


@pytest.mark.slow
def test_changing_the_model_makes_descriptions_stale(env, off):
    from tests.test_reconciler import _counts, _describe, _library
    from tests.test_lineage_api import _want

    lib = _library(env, "VisionStale")
    fake, _ = _endpoint()
    with fake:
        _save(env, api_url=URL, model="qwen3-vl:8b")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha, None)
    assert _want(env, "vision", sha)["settings_hash"]
    _describe(lib, clip, sha)
    assert _counts(lib, "vision")["current"] == 1
    with fake:
        _save(env, api_url=URL, model="llava:13b")
    assert _counts(lib, "vision")["stale"] == 1
