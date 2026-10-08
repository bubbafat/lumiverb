"""`lumiverb producers`: each producer's counts, and upgrading stale artifacts.

Mirrors Settings → Processing: the same counts, the same questions. The API
asks (409) about clips with a person's edits and, for producers that must
stay one model, for a confirmation naming the count; the CLI shows the
facts and asks, or with --yes says which flag answers.
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
         "upgradable": True, "why_not": None, "edited": 0, "upgrades": []}
    p.update(over)
    return p


def _response(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.text = str(body)
    return r


def _decision(code: str, message: str, details: dict) -> MagicMock:
    return _response(409, {"error": {"code": code, "message": message, "details": details}})


def _ok(**over) -> MagicMock:
    body = {"upgrade_id": "upg_1", "artifact": "vision", "scope": {"kind": "all", "id": None, "name": None},
            "edits": "keep", "upgrading": 3, "skipped_edited": 0, "replaced_edits": 0}
    body.update(over)
    return _response(200, body)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mod = importlib.import_module("src.client.cli.commands.producers")
    c = MagicMock()
    c.get.side_effect = _get
    monkeypatch.setattr(mod, "LumiverbClient", lambda: c)
    return c


LIBRARIES = [{"library_id": "lib_1", "name": "Footage"}]
PROJECTS = {"items": [{"project_id": "col_1", "name": "Wedding"}]}


def _get(path, params=None):
    r = MagicMock()
    if path == "/v1/libraries":
        r.json.return_value = LIBRARIES
    elif path == "/v1/projects":
        r.json.return_value = PROJECTS
    else:
        r.json.return_value = {"producers": [_producer()]}
    return r


def _run(*args: str, input: str | None = None):
    main = importlib.import_module("src.client.cli.main")
    return CliRunner().invoke(main.app, ["producers", *args], input=input)


def _posted(client) -> list[dict]:
    return [c.kwargs["json"] for c in client.raw.call_args_list if c.args[0] == "POST"]


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------


def test_lists_each_producer_with_its_counts(client):
    def get(path, params=None):
        r = MagicMock()
        r.json.return_value = {"producers": [
            _producer(edited=2, upgrades=[{"upgrade_id": "upg_1", "scope": {"kind": "library", "id": "lib_1",
                                                                              "name": "Footage"},
                                           "edits": "keep", "approved_at": "2026-10-08T22:00:00Z",
                                           "total": 5, "remaining": 2}]),
            _producer("scenes", "Scenes", upgradable=False, why_not="Not yet.",
                      counts={"applicable": 4, "current": 2, "stale": 2, "missing": 0, "failing": 0}),
        ]}
        return r

    client.get.side_effect = get
    result = _run()
    assert result.exit_code == 0, result.output
    client.get.assert_called_once_with("/v1/producers", params={})
    out = result.output
    assert "Descriptions and tags" in out and "vision" in out
    assert "upgrading in Footage, 2 left of 5" in out
    assert "2 of the stale have your edits" in out
    assert "Scenes can't be upgraded yet: Not yet." in out


def test_lists_one_library_or_project(client):
    assert _run("--library", "Footage").exit_code == 0
    client.get.assert_called_with("/v1/producers", params={"library_id": "lib_1"})
    assert _run("--project", "Wedding").exit_code == 0
    client.get.assert_called_with("/v1/producers", params={"project_id": "col_1"})


def test_an_unknown_library_or_project(client):
    result = _run("--library", "Nope")
    assert result.exit_code == 1 and "Library not found" in result.output
    result = _run("upgrade", "vision", "--project", "Nope")
    assert result.exit_code == 1 and "Project not found" in result.output
    client.raw.assert_not_called()


# ---------------------------------------------------------------------------
# Upgrading
# ---------------------------------------------------------------------------


def test_upgrade_everything(client):
    client.raw.return_value = _ok()
    result = _run("upgrade", "vision")
    assert result.exit_code == 0, result.output
    assert _posted(client) == [{}]
    client.raw.assert_called_with("POST", "/v1/producers/vision/upgrade", json={})
    assert "Upgrading 3 clips' vision" in result.output or "Upgrading 3 clips" in result.output


def test_upgrade_narrowed(client):
    client.raw.return_value = _ok(scope={"kind": "library", "id": "lib_1", "name": "Footage"})
    result = _run("upgrade", "vision", "--library", "Footage")
    assert result.exit_code == 0, result.output
    assert _posted(client) == [{"library_id": "lib_1"}]
    assert "in Footage" in result.output


def test_edited_clips_are_asked_about(client):
    client.raw.side_effect = [
        _decision("edited_clips", "2 of the 3 clips have your edits.",
                  {"stale": 3, "edited": 2, "choices": ["keep", "replace", "skip"]}),
        _ok(edits="skip", upgrading=1, skipped_edited=2),
    ]
    result = _run("upgrade", "vision", input="skip\n")
    assert result.exit_code == 0, result.output
    assert "2 of the 3 clips have your edits" in result.output
    assert _posted(client) == [{}, {"edits": "skip"}]
    assert "Skipped 2 clips with your edits" in result.output


def test_edited_clips_default_to_keeping_the_edits(client):
    client.raw.side_effect = [
        _decision("edited_clips", "1 of the 3 clips has your edits.", {"stale": 3, "edited": 1,
                                                                       "choices": ["keep", "replace", "skip"]}),
        _ok(),
    ]
    result = _run("upgrade", "vision", input="\n")
    assert result.exit_code == 0, result.output
    assert _posted(client)[-1] == {"edits": "keep"}


def test_replace_says_where_the_edits_went(client):
    client.raw.return_value = _ok(edits="replace", replaced_edits=2)
    result = _run("upgrade", "vision", "--edits", "replace")
    assert result.exit_code == 0, result.output
    assert _posted(client) == [{"edits": "replace"}]
    assert "Moved 2 clips' edits to history" in result.output


def test_with_yes_nobody_is_asked(client):
    client.raw.return_value = _decision("edited_clips", "2 of the 3 clips have your edits.",
                                        {"stale": 3, "edited": 2, "choices": ["keep", "replace", "skip"]})
    result = _run("upgrade", "vision", "--yes")
    assert result.exit_code == 2
    assert "--edits keep, --edits replace or --edits skip" in result.output
    assert len(_posted(client)) == 1


def test_everything_at_once_is_confirmed_with_the_count(client):
    client.raw.side_effect = [
        _decision("redo_everything", "All 1,200 clips' visual search (clip) will be made again.",
                  {"artifact": "clip", "title": "Visual search (CLIP)", "stale": 1200}),
        _ok(artifact="clip", upgrading=1200),
    ]
    result = _run("upgrade", "clip", input="y\n")
    assert result.exit_code == 0, result.output
    assert "Redo all 1,200?" in result.output
    assert _posted(client) == [{}, {"confirm": True}]


def test_everything_at_once_declined(client):
    client.raw.side_effect = [_decision("redo_everything", "All 5 clips' faces will be made again.",
                                        {"artifact": "faces", "title": "Faces", "stale": 5})]
    result = _run("upgrade", "faces", input="n\n")
    assert result.exit_code == 0
    assert len(_posted(client)) == 1
    assert "Nothing changed" in result.output


def test_everything_at_once_with_yes_needs_the_flag(client):
    client.raw.return_value = _decision("redo_everything", "All 5 clips' faces will be made again.",
                                        {"artifact": "faces", "title": "Faces", "stale": 5})
    result = _run("upgrade", "faces", "--yes")
    assert result.exit_code == 2 and "--redo-everything" in result.output
    client.raw.reset_mock()
    client.raw.return_value = _ok(artifact="faces", upgrading=5)
    assert _run("upgrade", "faces", "--redo-everything").exit_code == 0
    assert _posted(client) == [{"confirm": True}]


@pytest.mark.parametrize("status,code,message", [
    (409, "nothing_stale", "Nothing was made with older settings."),
    (409, "cant_upgrade", "Scenes can't be made again yet."),
    (422, "all_or_nothing", "Faces is upgraded everywhere or not at all."),
    (403, None, "Admin access required"),
])
def test_refusals_say_why(client, status, code, message):
    body = {"error": {"code": code, "message": message, "details": {}}} if code else {"detail": message}
    client.raw.return_value = _response(status, body)
    result = _run("upgrade", "vision")
    assert result.exit_code == 1
    assert message in result.output


def test_library_and_project_together_are_refused_before_asking(client):
    result = _run("upgrade", "vision", "--library", "Footage", "--project", "Wedding")
    assert result.exit_code == 2
    client.raw.assert_not_called()


# ---------------------------------------------------------------------------
# Cancelling
# ---------------------------------------------------------------------------


def test_cancel(client):
    client.raw.return_value = _response(204)
    result = _run("cancel", "vision")
    assert result.exit_code == 0, result.output
    client.raw.assert_called_once_with("DELETE", "/v1/producers/vision/upgrade", params={})
    assert "stay stale" in result.output


def test_cancel_one(client):
    client.raw.return_value = _response(404, {"detail": "No such upgrade"})
    result = _run("cancel", "vision", "--upgrade", "upg_9")
    assert result.exit_code == 1 and "No such upgrade" in result.output
    client.raw.assert_called_once_with("DELETE", "/v1/producers/vision/upgrade", params={"upgrade_id": "upg_9"})
