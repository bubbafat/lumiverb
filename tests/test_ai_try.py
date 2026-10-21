# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Settings › AI's Test (POST /v1/ai/machines/{id}/try): a machine tried
before it gets the work (the demo server, 10-21: a thinking model spent
scene descriptions' 500 tokens before answering, and a temperature it
refused showed only once the batch ran). It's sent what descriptions &
text's work sends, with the account's settings: a picture Lumiverb draws and
a video's scenes, one request for each producer's settings. The machine is
faked; everything else is the real API."""

from __future__ import annotations

import base64
import io
import json
from unittest.mock import MagicMock, patch

import pytest
import requests
from PIL import Image

from src.server import ai_sample
from tests.test_ai_machines import (  # noqa: F401
    GAMING_PC,
    QWEN,
    _add,
    _ai,
    _machine,
    _machines,
    none,
)
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_system_health_api import _server_admin

pytestmark = pytest.mark.slow

DESCRIBED = {"description": "A red house on a green hill, with a sign.", "tags": ["house", "hill", "sign"]}


def _answering(*, finish: str = "stop", status: int = 200, text: str = ""):
    """A machine's /chat/completions: each request kept, and answered by what it was asked (its text read, a
    description and tags), with the tokens it used; or with this status and text."""
    asked: list[dict] = []

    def post(url, json=None, headers=None, timeout=None):
        asked.append({"url": url, "json": json, "headers": headers or {}, "timeout": timeout})
        resp = MagicMock(status_code=status, ok=status < 400, text=text)
        if status >= 400:
            resp.json.return_value = {"error": {"message": text}}
            resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
            return resp
        prompt = json["messages"][0]["content"][-1]["text"]
        reply = ai_sample.SIGN if "text" in prompt.lower() and "describe" not in prompt.lower() else \
            json_dumps(DESCRIBED)
        resp.json.return_value = {"choices": [{"message": {"content": reply}, "finish_reason": finish}],
                                  "usage": {"prompt_tokens": 1200, "completion_tokens": 64, "total_tokens": 1264}}
        return resp

    return patch("src.processing.workers.captions.openai_caption.requests.post", side_effect=post), asked


def json_dumps(value) -> str:
    return json.dumps(value)


def _gaming_pc(env) -> str:
    fake, _ = _machines({GAMING_PC: (QWEN,)})
    with fake:
        assert _add(env, name="Gaming PC", api_url=GAMING_PC, jobs=["vision"]).status_code == 201
        client, headers, *_ = env
        assert client.put("/v1/ai/jobs/vision", json={"model": QWEN, "redo": False}, headers=headers).status_code == 200
    return _machine(_ai(env), "Gaming PC")["machine_id"]


def _try(env, machine_id: str, headers=None, **body):
    client, own, *_ = env
    return client.post(f"/v1/ai/machines/{machine_id}/try", json=body, headers=headers or own)


def test_a_machine_is_sent_what_the_work_sends_and_says_what_came_back(env, none):
    machine_id = _gaming_pc(env)
    answering, asked = _answering()
    with answering, patch("src.shared.vision_endpoint._blocked", return_value=False):
        r = _try(env, machine_id)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["machine"], out["model"]) == ("Gaming PC", QWEN) and "OPEN 9 TO 5" in out["sent"]
    tries = {", ".join(t["titles"]): t for t in out["tries"]}
    # Descriptions and scene descriptions have the same settings: one request for both.
    assert set(tries) == {"Scene descriptions, Descriptions and tags", "Video descriptions", "Text in images (OCR)"}
    described = tries["Scene descriptions, Descriptions and tags"]
    assert described["result"] == DESCRIBED and described["problem"] == "" and described["finish"] == "stop"
    assert described["tokens"] == {"prompt": 1200, "answer": 64} and described["limit"] == 500
    assert tries["Text in images (OCR)"]["result"] == "OPEN 9 TO 5"
    assert tries["Video descriptions"]["limit"] == 900 and tries["Video descriptions"]["result"] == DESCRIBED

    assert len(asked) == 3 and {a["url"] for a in asked} == {GAMING_PC + "/chat/completions"}
    assert all(a["json"]["model"] == QWEN and a["timeout"] == 45 for a in asked)
    pictures = [part["image_url"]["url"] for a in asked for part in a["json"]["messages"][0]["content"]
                if part["type"] == "image_url"]
    assert len(pictures) == 2, "the scenes go as text"
    with Image.open(io.BytesIO(base64.b64decode(pictures[0].split(",", 1)[1]))) as img:
        assert (img.format, img.size) == ("JPEG", (1280, 800)), "the picture Lumiverb draws, at the work's size"
    scenes = next(a for a in asked if len(a["json"]["messages"][0]["content"]) == 1)
    assert ai_sample.SCENES in scenes["json"]["messages"][0]["content"][0]["text"]


def test_a_thinking_model_that_runs_out_of_room_is_said(env, none):
    machine_id = _gaming_pc(env)
    answering, _ = _answering(finish="length")
    with answering, patch("src.shared.vision_endpoint._blocked", return_value=False):
        out = _try(env, machine_id).json()
    described = next(t for t in out["tries"] if "Descriptions and tags" in t["titles"])
    assert described["problem"].startswith("It ran out of room: it used all 500 tokens of its answer")


def test_a_refusal_is_said_its_own_words_to_a_server_admin(env, none):
    machine_id = _gaming_pc(env)
    answering, _ = _answering(status=400, text="max_tokens: 500 is too small for this model")
    with answering, patch("src.shared.vision_endpoint._blocked", return_value=False):
        theirs = _try(env, machine_id).json()
        full = _try(env, machine_id, headers=_server_admin(env)).json()
    assert {t["problem"] for t in theirs["tries"]} == {"It answered HTTP 400."}
    assert {t["problem"] for t in full["tries"]} == {
        "It answered HTTP 400: max_tokens: 500 is too small for this model"}


def test_another_model_can_be_tried_and_none_is_said(env, none):
    machine_id = _gaming_pc(env)
    answering, asked = _answering()
    with answering, patch("src.shared.vision_endpoint._blocked", return_value=False):
        assert _try(env, machine_id, model="llava:13b").status_code == 200
    assert {a["json"]["model"] for a in asked} == {"llava:13b"}

    client, headers, *_ = env
    assert client.put("/v1/ai/jobs/vision", json={"model": ""}, headers=headers).status_code == 200
    r = _try(env, machine_id)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_model", r.text
    built_in = next(m for m in _ai(env)["machines"] if m["built_in"])["machine_id"]
    r = _try(env, built_in, model=QWEN)
    assert r.status_code == 409 and r.json()["error"]["code"] == "built_in_machine", r.text


def test_the_sample_picture_is_the_same_every_time():
    assert ai_sample.picture() == ai_sample.picture()
    with Image.open(io.BytesIO(ai_sample.picture())) as img:
        assert img.size == (1280, 800)
