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


@pytest.mark.parametrize("cap,shown", [(None, "whole video"), (30, "first 30 seconds")])
def test_show(client, cap, shown):
    client.get.return_value.json.return_value = {"video_preview_max_seconds": cap}
    result = _run("show")
    assert result.exit_code == 0, result.output
    client.get.assert_called_once_with("/v1/tenant/settings")
    assert f"Video playback: {shown}" in result.output


@pytest.mark.parametrize("arg,value,shown", [("full", None, "whole video"), ("FULL", None, "whole video"),
                                             ("30", 30, "first 30 seconds")])
def test_set_video_preview(client, arg, value, shown):
    client.patch.return_value.json.return_value = {"video_preview_max_seconds": value}
    result = _run("video-preview", arg)
    assert result.exit_code == 0, result.output
    client.patch.assert_called_once_with("/v1/tenant/settings", json={"video_preview_max_seconds": value})
    assert f"Video playback: {shown}" in result.output


@pytest.mark.parametrize("arg", ["0", "-5", "2.5", "ten", "86401"])
def test_nonsense_is_refused_before_asking_the_server(client, arg):
    result = _run("video-preview", "--", arg)
    assert result.exit_code != 0
    client.patch.assert_not_called()
    assert "full" in result.output and "seconds" in result.output
