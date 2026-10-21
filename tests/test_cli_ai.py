# ruff: noqa: F811 — pytest fixtures are named as parameters
"""lumiverb ai: Settings › AI on the command line, for a server with no screen
(the demo server, 10-21: adding a machine meant a tunnel to the web app).
Each command against the real API, the machines' model lists faked
(tests/test_ai_machines.py). A key is asked for, never an argument, and
never shown."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from src.client.cli.commands import ai
from src.client.cli.main import app
from tests.derived import saved
from tests.test_ai_machines import (  # noqa: F401
    GAMING_PC,
    LLAVA,
    QWEN,
    STUDIO,
    _ai,
    _machine,
    _machines,
    none,
)
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _ingest_with, _sha, _want
from tests.test_reconciler import _library

pytestmark = pytest.mark.slow

runner = CliRunner()
KEY = "sk-never-shown-0123456789"


@pytest.fixture
def cli(env, monkeypatch):
    """The CLI, as the account's admin key, talking to the app in-process."""
    from fastapi.testclient import TestClient

    from src.client.cli.client import LumiverbClient
    from src.server.api.main import app as api

    _, headers, *_ = env
    client = LumiverbClient(base_url="http://testserver", token=headers["Authorization"].removeprefix("Bearer "))
    client._client = TestClient(api, headers=headers)
    monkeypatch.setattr(ai, "LumiverbClient", lambda: client)

    def run(*args: str, typed: str | None = None):
        return runner.invoke(app, ["ai", *args], input=typed)

    return run


def test_a_machine_is_added_with_its_key_asked_for_and_listed_without_it(env, none, cli):
    fake, seen = _machines({GAMING_PC: (QWEN, LLAVA)})
    with fake:
        added = cli("machines", "add", "Gaming PC", "--url", GAMING_PC, "--jobs", "vision", "--key", typed=KEY + "\n")
    assert added.exit_code == 0, added.output
    assert seen[-1]["headers"]["Authorization"] == f"Bearer {KEY}", "the key it was given"
    m = _machine(_ai(env), "Gaming PC")
    assert (m["api_url"], m["jobs"], m["has_key"], m["at_once"]) == (GAMING_PC, ["vision"], True, 2)

    listed = cli("machines", "list")
    assert listed.exit_code == 0 and "Gaming PC" in listed.output and "set" in listed.output
    assert KEY not in added.output + listed.output, "a key is never shown"
    with fake:
        offers = cli("machines", "models", "gaming pc")  # its saved key, by its name in any case
    assert offers.exit_code == 0 and offers.output.split() == [LLAVA, QWEN]


def test_what_goes_wrong_is_said(env, none, cli):
    fake, _ = _machines({})
    with fake:
        refused = cli("machines", "add", "Gone", "--url", STUDIO, "--jobs", "vision")
    assert refused.exit_code == 1 and "Couldn't add it: Couldn't reach the AI machine." in refused.output
    assert cli("machines", "add", "X", "--url", STUDIO, "--jobs", "painting").exit_code == 2
    unknown = cli("machines", "remove", "Nobody", "--yes")
    assert unknown.exit_code == 1 and "There's no machine called Nobody" in unknown.output


def test_a_machine_is_changed_by_what_is_given_and_its_key_never_goes_to_a_new_url(env, none, cli):
    fake, seen = _machines({GAMING_PC: (QWEN,), STUDIO: (QWEN,)})
    with fake:
        assert cli("machines", "add", "PC", "--url", GAMING_PC, "--jobs", "vision", "--key", typed=KEY).exit_code == 0
        moved = cli("machines", "edit", "PC", "--url", STUDIO, "--at-once", "4")
    assert moved.exit_code == 0, moved.output
    assert "Its key wasn't sent to the new URL" in moved.output
    assert "Authorization" not in seen[-1]["headers"], "asked at its new URL without the old key"
    m = _machine(_ai(env), "PC")
    assert (m["api_url"], m["at_once"], m["has_key"]) == (STUDIO, 4, False)
    with fake:
        assert cli("machines", "edit", "PC", "--name", "Studio", "--off").exit_code == 0
    assert _machine(_ai(env), "Studio")["enabled"] is False
    assert cli("machines", "edit", "Studio").exit_code == 2, "nothing to change"
    assert cli("machines", "remove", "Studio", typed="n\n").exit_code == 0 and _machine(_ai(env), "Studio")
    assert cli("machines", "remove", "Studio", "--yes").exit_code == 0
    assert [m["name"] for m in _ai(env)["machines"] if not m["built_in"]] == []


def _described(env) -> str:
    """A photo described with the vision model there is now."""
    import uuid

    lib = _library(env, f"Described {uuid.uuid4().hex[:8]}")
    sha = _sha()
    clip = _ingest_with(lib, "a.jpg", sha)
    r = saved(env[0], "vision", clip, headers=env[1], json={
        "model_id": "m", "description": "a dog", "lineage": _want(lib, "lumiverb.image.description", sha)})
    assert r.status_code == 200, r.text
    return clip


def test_a_jobs_model_is_shown_and_set_asking_what_becomes_of_what_the_old_one_made(env, none, cli):
    fake, _ = _machines({GAMING_PC: (QWEN, LLAVA)})
    with fake:
        assert cli("machines", "add", "PC", "--url", GAMING_PC, "--jobs", "vision").exit_code == 0
        assert cli("model", "vision", QWEN, "--new-only").exit_code == 0
        shown = cli("model", "vision")
        assert shown.exit_code == 0 and QWEN in shown.output and LLAVA in shown.output
        _described(env)  # a clip described with the model now
        kept = cli("model", "vision", LLAVA, typed="c\n")  # asked: change nothing
        assert kept.exit_code == 0 and "made with the old model" in kept.output and "Nothing changed." in kept.output
        assert next(j for j in _ai(env)["jobs"] if j["job"] == "vision")["model"] == QWEN
        changed = cli("model", "vision", LLAVA, typed="n\n")  # new files only
        assert changed.exit_code == 0, changed.output
        assert next(j for j in _ai(env)["jobs"] if j["job"] == "vision")["model"] == LLAVA
        missing = cli("model", "vision", "gpt-9", "--new-only")
    assert missing.exit_code == 1 and "offers gpt-9" in missing.output and f"PC: {LLAVA}, {QWEN}" in missing.output
    assert cli("model", "vision", "--off").exit_code == 0
    assert next(j for j in _ai(env)["jobs"] if j["job"] == "vision")["model"] == ""


def test_a_machine_is_tested_from_here_too(env, none, cli):
    from tests.test_ai_try import _answering

    fake, _ = _machines({GAMING_PC: (QWEN,)})
    with fake:
        assert cli("machines", "add", "PC", "--url", GAMING_PC, "--jobs", "vision").exit_code == 0
        assert cli("model", "vision", QWEN, "--new-only").exit_code == 0
    answering, asked = _answering()
    with answering, patch("src.shared.vision_endpoint._blocked", return_value=False):
        tested = cli("machines", "test", "PC")
    assert tested.exit_code == 0, tested.output
    assert "Sent a picture Lumiverb draws" in tested.output and "answered as the work needs it" in tested.output
    assert "64 of 500 tokens" in tested.output and len(asked) == 3
    thinking, _ = _answering(finish="length")
    with thinking, patch("src.shared.vision_endpoint._blocked", return_value=False):
        ran_out = cli("machines", "test", "PC")
    assert ran_out.exit_code == 1 and "It ran out of room" in ran_out.output
