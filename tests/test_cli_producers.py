"""`lumiverb producers`: each producer's counts, and stopping or resuming its redo.

Mirrors Settings → Processing (ADR-016 phase 4): stale clips are made again
after anything missing, since changing a setting was the approval; an admin
can stop that per producer and resume it.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.fast


def _producer(artifact="vision", title="Descriptions and tags", **over) -> dict:
    p = {"artifact": artifact, "producer": artifact, "version": "1", "title": title, "media": ["image"],
         "uniform": False, "settings": {"model": "qwen3-vl:8b-instruct"}, "settings_hash": "h",
         "counts": {"applicable": 10, "current": 6, "stale": 3, "missing": 1, "failing": 0},
         "redoable": True, "why_not": None, "paused": False, "paused_by": None, "paused_at": None}
    p.update(over)
    return p


def _response(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.text = str(body)
    return r


LIBRARIES = [{"library_id": "lib_1", "name": "Footage"}]
PROJECTS = {"items": [{"project_id": "col_1", "name": "Wedding"}]}


def _get(path, params=None):
    r = MagicMock()
    if path == "/v1/libraries":
        r.json.return_value = LIBRARIES
    elif path == "/v1/projects":
        assert params == {"status": "all"}  # archived projects can be named too
        r.json.return_value = PROJECTS
    else:
        r.json.return_value = {"producers": [_producer()]}
    return r


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mod = importlib.import_module("src.client.cli.commands.producers")
    c = MagicMock()
    c.get.side_effect = _get
    monkeypatch.setattr(mod, "LumiverbClient", lambda: c)
    return c


def _run(*args: str):
    main = importlib.import_module("src.client.cli.main")
    return CliRunner().invoke(main.app, ["producers", *args])


def test_lists_each_producer_with_its_counts_and_its_redo(client):
    def get(path, params=None):
        r = MagicMock()
        r.json.return_value = {"producers": [
            _producer(),
            _producer("ocr", "Text in images (OCR)", paused=True),
            _producer("scenes", "Scenes", redoable=False, why_not="Not yet."),
            _producer("clip", "Visual search (CLIP)",
                      counts={"applicable": 4, "current": 4, "stale": 0, "missing": 0, "failing": 0}),
        ]}
        return r

    client.get.side_effect = get
    result = _run()
    assert result.exit_code == 0, result.output
    client.get.assert_called_once_with("/v1/producers", params={})
    out = result.output
    assert "Descriptions and tags" in out and "redoing" in out
    assert "stopped" in out and "lumiverb producers resume ocr" in out
    assert "Scenes isn't made again yet: Not yet." in out
    assert "after anything missing" in out


def test_lists_one_library_or_project(client):
    assert _run("--library", "Footage").exit_code == 0
    client.get.assert_called_with("/v1/producers", params={"library_id": "lib_1"})
    assert _run("--project", "Wedding").exit_code == 0
    client.get.assert_called_with("/v1/producers", params={"project_id": "col_1"})


def test_an_unknown_library_or_project(client):
    result = _run("--library", "Nope")
    assert result.exit_code == 1 and "Library not found" in result.output
    result = _run("--project", "Nope")
    assert result.exit_code == 1 and "Project not found" in result.output


def test_library_and_project_together_are_refused(client):
    result = _run("--library", "Footage", "--project", "Wedding")
    assert result.exit_code == 2


def test_stop_and_resume(client):
    client.raw.return_value = _response(204)
    result = _run("stop", "vision")
    assert result.exit_code == 0, result.output
    client.raw.assert_called_with("POST", "/v1/producers/vision/redo/stop")
    assert "Stopped redoing vision" in result.output
    result = _run("resume", "vision")
    assert result.exit_code == 0, result.output
    client.raw.assert_called_with("POST", "/v1/producers/vision/redo/resume")
    assert "Redoing vision again" in result.output


@pytest.mark.parametrize(("status", "body", "says"), [
    (409, {"error": {"code": "cant_redo", "message": "Scenes isn't made again yet."}}, "Scenes isn't made again yet."),
    (403, {"detail": "Admins only"}, "Admins only"),
    (404, {"detail": "No such artifact"}, "No such artifact"),
])
def test_refusals_say_why(client, status, body, says):
    client.raw.return_value = _response(status, body)
    result = _run("stop", "scenes")
    assert result.exit_code == 1 and says in result.output


def test_the_upgrade_commands_are_gone(client):
    assert _run("upgrade", "vision").exit_code != 0
    assert _run("cancel", "vision").exit_code != 0
