"""`lumiverb producers`: each producer's counts, pausing and resuming, and stopping or resuming a redo.

Mirrors Settings → Processing (ADR-016 phase 4): stale clips are made again
after anything missing, since changing a setting was the approval; an admin
can stop that per producer and resume it. An admin can also pause all
processing, or one producer's, and resume it (Robert, Oct 9).
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
         "redoable": True, "why_not": None, "redo_stopped": False, "redo_stopped_by": None, "redo_stopped_at": None,
         "paused": False, "paused_by": None, "paused_at": None}
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
            _producer("ocr", "Text in images (OCR)", redo_stopped=True),
            _producer("faces", "Faces", paused=True),
            _producer("scenes", "Scenes", redoable=False, why_not="Not yet."),
            _producer("clip", "Visual search (CLIP)",
                      counts={"applicable": 4, "current": 4, "stale": 0, "missing": 0, "failing": 0}),
        ]}
        return r

    client.get.side_effect = get
    result = _run()
    assert result.exit_code == 0, result.output
    client.get.assert_any_call("/v1/producers", params={})
    out = result.output
    assert "Descriptions and tags" in out and "redoing" in out
    assert "stopped" in out and "lumiverb producers redo resume ocr" in out
    assert "Faces: paused" in out and "lumiverb producers resume faces" in out
    assert "All processing is paused" not in out
    assert "Scenes isn't made again yet: Not yet." in out
    assert "after anything missing" in out


def test_lists_one_library_or_project(client):
    assert _run("--library", "Footage").exit_code == 0
    client.get.assert_any_call("/v1/producers", params={"library_id": "lib_1"})
    assert _run("--project", "Wedding").exit_code == 0
    client.get.assert_any_call("/v1/producers", params={"project_id": "col_1"})


def test_an_unknown_library_or_project(client):
    result = _run("--library", "Nope")
    assert result.exit_code == 1 and "Library not found" in result.output
    result = _run("--project", "Nope")
    assert result.exit_code == 1 and "Project not found" in result.output


def test_library_and_project_together_are_refused(client):
    result = _run("--library", "Footage", "--project", "Wedding")
    assert result.exit_code == 2


def test_the_listing_says_when_all_processing_is_paused(client):
    def get(path, params=None):
        r = MagicMock()
        r.json.return_value = ({"paused": True, "paused_at": "2026-10-09T10:00:00Z"} if path == "/v1/producers/queue"
                               else {"producers": [_producer()]})
        return r

    client.get.side_effect = get
    result = _run()
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "All processing is paused (since 2026-10-09 10:00)" in out and "lumiverb producers resume carries on" in out


def test_the_redo_column_says_a_paused_producers_redo_waits():
    mod = importlib.import_module("src.client.cli.commands.producers")
    assert mod._redo(_producer(), all_paused=False) == "redoing"
    assert mod._redo(_producer(paused=True), all_paused=False) == "paused"
    assert mod._redo(_producer(), all_paused=True) == "paused"
    assert mod._redo(_producer(redo_stopped=True), all_paused=True) == "stopped"


def test_while_all_is_paused_the_listing_names_no_paused_part(client):
    # Resume all turns everything on, so a part paused alone isn't worth naming meanwhile.
    def get(path, params=None):
        r = MagicMock()
        r.json.return_value = ({"paused": True, "scans_paused": True} if path == "/v1/producers/queue"
                               else {"producers": [_producer("faces", "Faces", paused=True)]})
        return r

    client.get.side_effect = get
    out = " ".join(_run().output.split())
    assert "All processing is paused" in out
    assert "Scans are paused" not in out and "Faces: paused" not in out


def test_the_listing_says_when_scans_are_paused(client):
    def get(path, params=None):
        r = MagicMock()
        r.json.return_value = ({"paused": False, "scans_paused": True, "scans_paused_at": "2026-10-09T11:00:00Z"}
                               if path == "/v1/producers/queue" else {"producers": [_producer()]})
        return r

    client.get.side_effect = get
    result = _run()
    out = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "Scans are paused (since 2026-10-09 11:00)" in out and "lumiverb producers resume scans" in out
    assert "All processing is paused" not in out


def test_pause_and_resume_all_processing_or_one_producer(client):
    client.raw.return_value = _response(204)
    for args, path, says in [
        (["pause"], "/v1/producers/pause", "Paused all processing"),
        (["pause", "vision"], "/v1/producers/vision/pause", "Paused vision"),
        (["resume"], "/v1/producers/resume", "Resumed all processing: scans and every producer are on again"),
        (["resume", "vision"], "/v1/producers/vision/resume", "Resumed vision"),
        (["pause", "scans"], "/v1/producers/scans/pause", "Paused scans: no new or changed files are found"),
        (["resume", "scans"], "/v1/producers/scans/resume", "Resumed scans"),
    ]:
        result = _run(*args)
        assert result.exit_code == 0, result.output
        client.raw.assert_called_with("POST", path)
        assert says in result.output, result.output


def test_stop_and_resume_a_redo(client):
    client.raw.return_value = _response(204)
    result = _run("redo", "stop", "vision")
    assert result.exit_code == 0, result.output
    client.raw.assert_called_with("POST", "/v1/producers/vision/redo/stop")
    assert "Stopped redoing vision" in result.output
    assert "lumiverb producers redo resume vision" in " ".join(result.output.split())
    result = _run("redo", "resume", "vision")
    assert result.exit_code == 0, result.output
    client.raw.assert_called_with("POST", "/v1/producers/vision/redo/resume")
    assert "Redoing vision again" in result.output


def test_the_old_stop_command_is_gone(client):
    client.raw.return_value = _response(204)
    assert _run("stop", "vision").exit_code == 2  # no such command
    client.raw.assert_not_called()


@pytest.mark.parametrize(("args", "status", "body", "says"), [
    (["redo", "stop", "scenes"], 409, {"error": {"code": "cant_redo", "message": "Scenes isn't made again yet."}},
     "Scenes isn't made again yet."),
    (["redo", "stop", "scenes"], 403, {"detail": "Admins only"}, "Admins only"),
    (["redo", "resume", "nope"], 404, {"detail": "No such artifact"}, "No such artifact"),
    (["pause", "proxy"], 409, {"error": {"code": "not_scheduled", "message": "Proxies and thumbnails are made by "
                                         "scans"}}, "made by scans"),
    (["pause"], 403, {"detail": "Admins only"}, "Admins only"),
    (["resume", "nope"], 404, {"detail": "No such artifact"}, "No such artifact"),
])
def test_refusals_say_why(client, args, status, body, says):
    client.raw.return_value = _response(status, body)
    result = _run(*args)
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


# --- lumiverb producers settings: mirrors the form in Settings → Processing

_FIELDS = [
    {"key": "model", "label": "Model", "kind": "text", "value": "small", "default": "small", "minimum": None,
     "maximum": None, "unit": "", "advanced": False, "fixed": "Chosen in Settings → AI, for every producer its machines run."},
    {"key": "vad_min_silence_ms", "label": "Shortest silence skipped", "kind": "int", "value": 500, "default": 500,
     "minimum": 100, "maximum": 2000, "unit": "ms", "advanced": False, "fixed": None},
]


@pytest.fixture
def settings_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mod = importlib.import_module("src.client.cli.commands.producers")
    c = MagicMock()
    c.get.return_value.json.return_value = {"producers": [
        _producer("transcript", "Transcripts", fields=_FIELDS, settings={"model": "small", "vad_min_silence_ms": 500})]}
    monkeypatch.setattr(mod, "LumiverbClient", lambda: c)
    return c


def _settings(*args: str, input: str | None = None):
    from src.client.cli.main import app

    return CliRunner().invoke(app, ["producers", "settings", *args], input=input)


def test_settings_lists_each_with_what_it_can_be(settings_client):
    result = _settings("transcript")
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())  # the table wraps at 80 columns
    assert "Shortest" in flat and "100–2000" in flat and "fixed" in flat


def test_settings_changes_one(settings_client):
    settings_client.raw.return_value = _response(200, {"settings": {"vad_min_silence_ms": 800}})
    result = _settings("transcript", "vad_min_silence_ms=800")
    assert result.exit_code == 0, result.output
    settings_client.raw.assert_called_once_with("PUT", "/v1/producers/transcript/settings",
                                                json={"settings": {"vad_min_silence_ms": 800}, "redo": False})
    assert "vad_min_silence_ms = 800" in result.output


def test_settings_asks_before_redoing_as_the_server_does(settings_client):
    settings_client.raw.side_effect = [
        _response(409, {"error": {"code": "redo_on_change", "message": "New settings make 12 clips of transcripts again."}}),
        _response(200, {"settings": {"vad_min_silence_ms": 800}}),
    ]
    result = _settings("transcript", "vad_min_silence_ms=800", input="y\n")
    assert result.exit_code == 0, result.output
    assert "12 clips" in result.output
    assert settings_client.raw.call_args_list[1].kwargs["json"] == {"settings": {"vad_min_silence_ms": 800}, "redo": True}


def test_settings_says_no_and_nothing_changes(settings_client):
    settings_client.raw.return_value = _response(
        409, {"error": {"code": "redo_on_change", "message": "New settings make 12 clips of transcripts again."}})
    result = _settings("transcript", "vad_min_silence_ms=800", input="n\n")
    assert result.exit_code == 0 and "Nothing changed" in result.output
    assert settings_client.raw.call_count == 1


def test_settings_puts_one_back_to_its_default(settings_client):
    settings_client.raw.return_value = _response(200, {"settings": {"vad_min_silence_ms": 500}})
    _settings("transcript", "vad_min_silence_ms=")
    assert settings_client.raw.call_args.kwargs["json"] == {"settings": {"vad_min_silence_ms": None}, "redo": False}


def test_settings_refuses_what_isnt_a_setting_or_a_number(settings_client):
    assert _settings("transcript", "silence=800").exit_code == 2
    assert _settings("transcript", "vad_min_silence_ms=lots").exit_code == 2
    assert _settings("teleport").exit_code == 2
    settings_client.raw.assert_not_called()


def test_settings_says_why_the_server_refused(settings_client):
    settings_client.raw.return_value = _response(
        422, {"error": {"code": "bad_setting", "message": "Shortest silence skipped is from 100 to 2000 ms"}})
    result = _settings("transcript", "vad_min_silence_ms=50")
    assert result.exit_code == 1 and "is from 100 to 2000 ms" in result.output


_AI_FIELDS = [
    {"key": "prompt", "label": "Prompt", "kind": "text", "value": "Describe it. " * 10 + "Answer as key=value.",
     "default": "Describe it.", "minimum": None, "maximum": None, "unit": "", "advanced": False, "fixed": None},
    {"key": "temperature", "label": "Temperature", "kind": "float", "value": 0.2, "default": 0.2, "minimum": 0,
     "maximum": 2, "unit": "", "advanced": True, "fixed": None},
]


@pytest.fixture
def vision_client(settings_client: MagicMock) -> MagicMock:
    settings_client.get.return_value.json.return_value = {"producers": [
        _producer("vision", "Descriptions and tags", fields=_AI_FIELDS, settings={})]}
    return settings_client


def test_settings_shows_a_long_prompt_whole_below_the_table(vision_client):
    result = _settings("vision")
    assert result.exit_code == 0, result.output
    assert "Answer as key=value." in " ".join(result.output.split())


def test_a_value_keeps_its_own_equals_signs(vision_client):
    vision_client.raw.return_value = _response(200, {"settings": {"prompt": "a=b, c=d"}})
    assert _settings("vision", "prompt=a=b, c=d").exit_code == 0
    assert vision_client.raw.call_args.kwargs["json"]["settings"] == {"prompt": "a=b, c=d"}


def test_yes_doesnt_ask(vision_client):
    vision_client.raw.return_value = _response(200, {"settings": {"temperature": 0.5}})
    result = _settings("vision", "temperature=0.5", "--yes")
    assert result.exit_code == 0, result.output
    vision_client.raw.assert_called_once_with("PUT", "/v1/producers/vision/settings",
                                              json={"settings": {"temperature": 0.5}, "redo": True})


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "1e400"])
def test_a_number_that_isnt_finite_is_refused_here(vision_client, raw):
    result = _settings("vision", f"temperature={raw}")
    assert result.exit_code == 2 and "finite number" in result.output, result.output
    vision_client.raw.assert_not_called()


def test_a_whole_number_may_be_written_800_point_0(settings_client):
    settings_client.raw.return_value = _response(200, {"settings": {"vad_min_silence_ms": 800}})
    assert _settings("transcript", "vad_min_silence_ms=800.0").exit_code == 0
    assert settings_client.raw.call_args.kwargs["json"]["settings"] == {"vad_min_silence_ms": 800}
    assert _settings("transcript", "vad_min_silence_ms=800.5").exit_code == 2


def test_a_fixed_setting_is_refused_saying_why(settings_client):
    settings_client.raw.return_value = _response(
        422, {"error": {"code": "setting_fixed", "message": "Model: Chosen in Settings → AI"}})
    result = _settings("transcript", "model=large-v3")
    assert result.exit_code == 1 and "Settings → AI" in result.output



def test_says_how_long_until_each_and_everything_is_made(client):
    def get(path, params=None):
        r = MagicMock()
        if path == "/v1/producers/queue":
            r.json.return_value = {"live": True, "eta": {"producers": {"vision": 7500.0, "ocr": None},
                                                          "pools": {}, "caught_up": 7500.0, "jobs": []}}
        else:
            r.json.return_value = {"producers": [_producer(), _producer("ocr", "Text in images (OCR)")]}
        return r

    client.get.side_effect = get
    result = _run()
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "Caught up in about 2 h 5 min." in flat and "2 h 5 min" in flat.split("Caught up")[0]


def test_one_library_says_no_time_left_its_the_whole_accounts(client):
    # The queue is still read: what's paused is the whole account's too, and said for one library.
    base = client.get.side_effect

    def get(path, params=None):
        if path == "/v1/producers/queue":
            r = MagicMock()
            r.json.return_value = {"live": True, "paused": True, "eta": {"producers": {"vision": 7500.0},
                                                                         "pools": {}, "caught_up": 7500.0, "jobs": []}}
            return r
        return base(path, params)

    client.get.side_effect = get
    result = _run("--library", "Footage")
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "Caught up" not in flat and "Left" not in flat and "All processing is paused" in flat


def test_the_headline_says_paused_work_gets_no_time(client):
    from src.client.cli.commands.producers import _caught_up

    assert _caught_up({"caught_up": None, "not_counted": [
        {"artifact": "vision", "title": "Descriptions and tags", "why": "paused"}]}) == (
        "What's left is paused: no time is promised until it's resumed.")
    assert _caught_up({"caught_up": 60.0 * 30, "not_counted": [
        {"artifact": "vision", "title": "Descriptions and tags", "why": "paused"}]}) == (
        "Caught up in about 30 min, not counting descriptions and tags (paused).")



def test_the_headline_names_what_it_leaves_out(client):
    from src.client.cli.commands.producers import _caught_up

    assert _caught_up({"caught_up": 7500.0, "not_counted": [
        {"artifact": "transcript", "title": "Transcripts", "why": "no_machine"}]}) == (
        "Caught up in about 2 h 5 min, not counting transcripts (no machine doing them now).")
    assert _caught_up({"caught_up": 40 * 86400.0}) == "Caught up in over a month."
    assert _caught_up({"caught_up": 0}) == "Caught up: everything is made."
