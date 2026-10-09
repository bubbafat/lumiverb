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


FAILING = {"items": [
    {"asset_id": "ast_1", "artifact": "vision", "title": "Descriptions and tags", "rel_path": "Day 1/a.jpg",
     "library_id": "lib_1", "library_name": "Footage", "media_type": "image", "error": "the model said nothing",
     "attempts": 10, "failed_at": "2026-10-09T01:00:00Z", "retry_at": None, "given_up": True},
    {"asset_id": "ast_2", "artifact": "ocr", "title": "Text in images (OCR)", "rel_path": "b.jpg",
     "library_id": "lib_1", "library_name": "Footage", "media_type": "image", "error": "timed out",
     "attempts": 2, "failed_at": "2026-10-09T01:00:00Z", "retry_at": "2026-10-09T01:10:00Z", "given_up": False}],
    "next_cursor": None}


def test_failures_say_why_and_when_the_next_try_is(client):
    client.raw.return_value = _response(200, FAILING)
    result = _run("failures")
    assert result.exit_code == 0, result.output
    # The table folds long cells at 80 columns: compare without the spacing and borders.
    out = "".join(ch for ch in result.output if ch.isalnum() or ch in ":-")
    assert "themodelsaid" in out and "givenup" in out and "2026-10-0901:10" in out and "timedout" in out
    assert client.raw.call_args.args == ("GET", "/v1/producers/failures")
    assert client.raw.call_args.kwargs["params"] == {"limit": 50}


def test_failures_of_one_producer_in_one_library(client):
    client.raw.return_value = _response(200, {"items": [], "next_cursor": None})
    result = _run("failures", "vision", "--library", "Footage")
    assert "Nothing is failing." in result.output
    assert client.raw.call_args.kwargs["params"] == {"limit": 50, "artifact": "vision", "library_id": "lib_1"}


def test_retry_some_or_all(client):
    client.raw.return_value = _response(200, {"retried": 1})
    result = _run("retry", "vision", "--asset", "ast_1")
    assert result.exit_code == 0 and "1 clip will be tried again shortly." in result.output
    assert client.raw.call_args.kwargs["json"] == {"artifact": "vision", "asset_ids": ["ast_1"]}
    client.raw.return_value = _response(200, {"retried": 0})
    result = _run("retry")
    assert "Nothing was failing." in result.output
    assert client.raw.call_args.kwargs["json"] == {}


def test_retry_refused_says_why(client):
    client.raw.return_value = _response(403, {"detail": "Editors only"})
    result = _run("retry")
    assert result.exit_code == 1 and "Editors only" in result.output
