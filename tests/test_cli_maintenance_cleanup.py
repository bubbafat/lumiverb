"""`lumiverb maintenance cleanup` says when the server skipped it because all
processing is paused, so it doesn't read as "nothing to clean up" (Robert, Oct 9)."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.fast

RESULT = {"orphan_tenants": 0, "orphan_libraries": 0, "orphan_files": 0, "bytes_freed": 0, "skipped_libraries": 0,
          "errors": [], "dry_run": False, "paused": False, "paused_tenants": []}


def _run(monkeypatch, result: dict) -> str:
    from src.client.cli.main import app

    mod = importlib.import_module("src.client.cli.commands.maintenance")
    c = MagicMock()
    c.post.return_value.json.return_value = result
    monkeypatch.setattr(mod, "LumiverbClient", lambda: c)
    r = CliRunner().invoke(app, ["maintenance", "cleanup", "--execute"])
    assert r.exit_code == 0, r.output
    return " ".join(r.output.split())


def test_a_cleanup_skipped_for_a_pause_says_so(monkeypatch):
    out = _run(monkeypatch, {**RESULT, "paused": True})
    assert "Skipped: all processing is paused" in out and "lumiverb producers resume" in out


def test_a_cleanup_with_nothing_to_do_says_no_pause(monkeypatch):
    assert "paused" not in _run(monkeypatch, RESULT)
