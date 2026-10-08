"""`lumiverb settings`: account-wide settings from the command line.

Mirrors the web's Settings → Playback: how much of each video plays.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.fast


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mod = importlib.import_module("src.client.cli.commands.settings")
    c = MagicMock()
    monkeypatch.setattr(mod, "LumiverbClient", lambda: c)
    return c


def _run(*args: str):
    main = importlib.import_module("src.client.cli.main")
    return CliRunner().invoke(main.app, ["settings", *args])


@pytest.mark.parametrize("cap,public,shown,public_shown", [
    (None, 10, "whole video", "first 10 seconds"),
    (30, None, "first 30 seconds", "first 30 seconds"),  # never more than signed in
    (None, None, "whole video", "whole video"),
])
def test_show(client, cap, public, shown, public_shown):
    client.get.return_value.json.return_value = {"video_preview_max_seconds": cap,
                                                 "public_video_preview_max_seconds": public}
    result = _run("show")
    assert result.exit_code == 0, result.output
    client.get.assert_called_once_with("/v1/tenant/settings")
    assert f"Video playback: {shown}" in result.output
    assert f"On public pages: {public_shown}" in result.output


@pytest.mark.parametrize("arg,value,shown", [("full", None, "whole video"), ("FULL", None, "whole video"),
                                             ("30", 30, "first 30 seconds")])
def test_set_video_preview(client, arg, value, shown):
    client.patch.return_value.json.return_value = {"video_preview_max_seconds": value}
    result = _run("video-preview", arg)
    assert result.exit_code == 0, result.output
    client.patch.assert_called_once_with("/v1/tenant/settings", json={"video_preview_max_seconds": value})
    assert f"Video playback: {shown}" in result.output


@pytest.mark.parametrize("arg,value,shown", [("full", None, "whole video"), ("45", 45, "first 45 seconds")])
def test_set_public_preview(client, arg, value, shown):
    client.patch.return_value.json.return_value = {"public_video_preview_max_seconds": value}
    result = _run("public-preview", arg)
    assert result.exit_code == 0, result.output
    client.patch.assert_called_once_with("/v1/tenant/settings", json={"public_video_preview_max_seconds": value})
    assert f"On public pages: {shown}" in result.output


@pytest.mark.parametrize("command", ["video-preview", "public-preview"])
@pytest.mark.parametrize("arg", ["0", "-5", "2.5", "ten", "86401", "\u00b2", "\u0663"])
def test_nonsense_is_refused_before_asking_the_server(client, command, arg):
    # Superscript and Arabic-Indic digits pass str.isdigit() but aren't seconds.
    result = _run(command, "--", arg)
    assert result.exit_code == 2, result.output
    client.patch.assert_not_called()
    assert "full" in result.output and "seconds" in result.output


def test_show_says_what_public_pages_actually_get(client):
    # Public pages never play more than signed-in people get.
    client.get.return_value.json.return_value = {"video_preview_max_seconds": 5,
                                                 "public_video_preview_max_seconds": 10}
    result = _run("show")
    assert "On public pages: first 5 seconds" in result.output


# ---------------------------------------------------------------------------
# Follow moves and renames (Robert's call, Oct 8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,shown", [(True, "on"), (False, "off"), (None, "on")])
def test_show_says_whether_moves_are_followed(client, value, shown):
    settings = {"video_preview_max_seconds": None, "public_video_preview_max_seconds": 10}
    if value is not None:
        settings["follow_moves"] = value
    client.get.return_value.json.return_value = settings
    result = _run("show")
    assert result.exit_code == 0, result.output
    assert f"Follow moves and renames: {shown}" in result.output


@pytest.mark.parametrize("arg,value", [("on", True), ("off", False), ("OFF", False)])
def test_follow_moves_turns_it_on_or_off(client, arg, value):
    client.patch.return_value.json.return_value = {"follow_moves": value}
    result = _run("follow-moves", arg)
    assert result.exit_code == 0, result.output
    client.patch.assert_called_once_with("/v1/tenant/settings", json={"follow_moves": value})
    assert f"Follow moves and renames: {'on' if value else 'off'}" in result.output
    if not value:
        assert "new path" in " ".join(result.output.split())  # says what off means


@pytest.mark.parametrize("arg", ["yes", "1", "maybe"])
def test_follow_moves_takes_on_or_off(client, arg):
    result = _run("follow-moves", arg)
    assert result.exit_code == 2, result.output
    client.patch.assert_not_called()
