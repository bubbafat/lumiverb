"""`lumiverb pause|resume <name>` (Robert, Oct 9): one pause switch (a
producer or upkeep), or `all` for every one; `scans`, every library's own
Scans pause, or one library's with --library (ADR-018 phase 1). With no
name it acts on nothing and lists the names. After acting it says the
account's state, as derived from its switches and its libraries' scans:
Running, Partly paused or Paused.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.fast

SWITCHES = [{"target": "upkeep", "title": "Upkeep"},
            {"target": "vision", "title": "Descriptions and tags"}, {"target": "ocr", "title": "Text in images (OCR)"}]
LIBRARIES = [{"library_id": "lib_1", "name": "Footage"}, {"library_id": "lib_2", "name": "Stills"}]


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
    (["resume", "upkeep"], "/v1/producers/upkeep/resume"),
    (["pause", "all"], "/v1/producers/all/pause"),
    (["resume", "all"], "/v1/producers/all/resume"),
])
def test_pause_or_resume_one_switch_or_all_and_say_the_state(client, args, path):
    result = _run(*args)
    assert result.exit_code == 0, result.output
    client.raw.assert_called_once_with("POST", path, json={"scope": "work"})
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


@pytest.mark.parametrize(("args", "body"), [
    (["pause", "scans"], {"all": True}),
    (["resume", "scans"], {"all": True}),
    (["pause", "scans", "--library", "Stills"], {"library_ids": ["lib_2"]}),
    (["resume", "scans", "--library", "lib_1"], {"library_ids": ["lib_1"]}),
])
def test_scans_are_each_librarys_every_one_or_one_named(client, args, body):
    queue = _response(200, {"state": "partly", "switches": [{**sw, "paused": False} for sw in SWITCHES],
                            "libraries": [{**LIBRARIES[1], "paused": True}]})
    client.get.side_effect = lambda path, **kw: _response(200, LIBRARIES) if path == "/v1/libraries" else queue
    result = _run(*args)
    assert result.exit_code == 0, result.output
    client.raw.assert_called_once_with("POST", f"/v1/libraries/scans/{args[0]}", json=body)
    assert "Paused: Scans of Stills" in " ".join(result.output.split())


def test_a_library_that_isnt_there_or_without_scans_acts_on_nothing(client):
    client.get.side_effect = lambda path, **kw: _response(200, LIBRARIES)
    result = _run("pause", "scans", "--library", "Nope")
    assert result.exit_code == 1 and "Library not found: Nope" in result.output
    result = _run("pause", "vision", "--library", "Footage")
    assert result.exit_code == 2 and "--library goes with scans" in result.output
    client.raw.assert_not_called()


@pytest.mark.parametrize(("args", "status", "body", "says"), [
    (["pause", "proxy"], 409, {"error": {"code": "not_scheduled", "message": "Proxies and thumbnails are made by "
                                         "scans: pausing a library's scans stops them."}}, "made by scans"),
    (["pause", "all"], 403, {"error": {"code": "forbidden", "message": "Admins only", "details": {}}}, "Admins only"),
    (["resume", "nope"], 404, {"error": {"code": "not_found", "message": "No such artifact", "details": {}}}, "No such artifact"),
])
def test_refusals_say_why(client, args, status, body, says):
    client.raw.return_value = _response(status, body)
    result = _run(*args)
    assert result.exit_code == 1 and says in result.output
