"""`lumiverb pause|resume <name>` (Robert, Oct 9): one pause switch (a
producer, scans or upkeep), or `all` for every one. With no name it acts on
nothing and lists the names. After acting it says the account's state, as
derived from its switches: Running, Partly paused or Paused.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.fast

SWITCHES = [{"target": "scans", "title": "Scans"}, {"target": "upkeep", "title": "Upkeep"},
            {"target": "vision", "title": "Descriptions and tags"}, {"target": "ocr", "title": "Text in images (OCR)"}]


def _response(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.text = str(body)
    return r


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mod = importlib.import_module("src.client.cli.commands.pausing")
    c = MagicMock()
    c.raw.return_value = _response(204)
    c.get.return_value = _response(200, {"state": "partly", "switches": [
        {**sw, "paused": sw["target"] == "vision"} for sw in SWITCHES]})
    monkeypatch.setattr(mod, "LumiverbClient", lambda: c)
    return c


def _run(*args: str):
    main = importlib.import_module("src.client.cli.main")
    return CliRunner().invoke(main.app, list(args))


@pytest.mark.parametrize(("args", "path"), [
    (["pause", "vision"], "/v1/producers/vision/pause"),
    (["resume", "vision"], "/v1/producers/vision/resume"),
    (["pause", "scans"], "/v1/producers/scans/pause"),
    (["resume", "upkeep"], "/v1/producers/upkeep/resume"),
    (["pause", "all"], "/v1/producers/all/pause"),
    (["resume", "all"], "/v1/producers/all/resume"),
])
def test_pause_or_resume_one_switch_or_all_and_say_the_state(client, args, path):
    result = _run(*args)
    assert result.exit_code == 0, result.output
    client.raw.assert_called_once_with("POST", path)
    out = " ".join(result.output.split())
    assert "Processing: Partly paused" in out and "Paused: Descriptions and tags" in out


def test_after_all_is_paused_it_says_paused(client):
    client.get.return_value = _response(200, {"state": "paused", "switches": [
        {**sw, "paused": True} for sw in SWITCHES]})
    out = " ".join(_run("pause", "all").output.split())
    assert "Processing: Paused" in out and "Paused:" not in out.replace("Processing: Paused", "")


@pytest.mark.parametrize("command", ["pause", "resume"])
def test_without_a_name_nothing_happens_and_the_names_are_listed(client, command):
    result = _run(command)
    assert result.exit_code == 2, result.output
    client.raw.assert_not_called()
    out = " ".join(result.output.split())
    assert f"lumiverb {command} <name>" in out
    assert "all, scans, upkeep, vision, ocr" in out


@pytest.mark.parametrize(("args", "status", "body", "says"), [
    (["pause", "proxy"], 409, {"error": {"code": "not_scheduled", "message": "Proxies and thumbnails are made by "
                                         "scans: pausing Scans stops them."}}, "made by scans"),
    (["pause", "all"], 403, {"detail": "Admins only"}, "Admins only"),
    (["resume", "nope"], 404, {"detail": "No such artifact"}, "No such artifact"),
])
def test_refusals_say_why(client, args, status, body, says):
    client.raw.return_value = _response(status, body)
    result = _run(*args)
    assert result.exit_code == 1 and says in result.output
