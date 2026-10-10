"""`lumiverb maintenance cleanup` says when the server skipped it because the
Upkeep switch is paused, so it doesn't read as "nothing to clean up" (Robert, Oct 9)."""

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
    r = CliRunner().invoke(app, ["maintenance", "cleanup", "--all", "--execute"])
    assert r.exit_code == 0, r.output
    return " ".join(r.output.split())


def test_a_cleanup_skipped_for_a_pause_says_so(monkeypatch):
    out = _run(monkeypatch, {**RESULT, "paused": True})
    assert "Skipped: Upkeep is paused" in out and "lumiverb resume upkeep" in out


def test_a_cleanup_with_nothing_to_do_says_no_pause(monkeypatch):
    assert "paused" not in _run(monkeypatch, RESULT)


# ---------------------------------------------------------------------------
# What each command acts on is said, never taken from a missing argument
# (Robert): --library or --all; what's said is what's sent.
# ---------------------------------------------------------------------------


def _client() -> MagicMock:
    c = MagicMock()
    c.get.return_value.json.return_value = [{"library_id": "lib_1", "name": "Media"}]
    c.post.return_value.json.return_value = {**RESULT, "synced": 0, "failed": 0, "deleted": 0}
    return c


def _invoke(monkeypatch, *args: str):
    from src.client.cli.main import app

    mod = importlib.import_module("src.client.cli.commands.maintenance")
    c = _client()
    monkeypatch.setattr(mod, "LumiverbClient", lambda: c)
    return CliRunner().invoke(app, ["maintenance", *args]), c


@pytest.mark.parametrize("args", [["cleanup"], ["cleanup", "--library", "Media", "--all"], ["search-sync"],
                                  ["search-sync", "--force"], ["cleanup-dismissed"]])
def test_nothing_runs_without_a_scope(monkeypatch, args):
    r, c = _invoke(monkeypatch, *args)
    assert r.exit_code == 2, r.output
    assert "--all" in r.output
    c.post.assert_not_called()


def test_cleanup_of_one_library_sends_it(monkeypatch):
    r, c = _invoke(monkeypatch, "cleanup", "--library", "Media")
    assert r.exit_code == 0, r.output
    c.post.assert_called_once_with("/v1/upkeep/cleanup", params={"dry_run": "true", "library_id": "lib_1"})


def test_cleanup_of_an_unknown_library_sends_nothing(monkeypatch):
    r, c = _invoke(monkeypatch, "cleanup", "--library", "Nope")
    assert r.exit_code == 1
    assert "Library not found" in r.output
    c.post.assert_not_called()


def test_cleanup_all_is_the_account(monkeypatch):
    r, c = _invoke(monkeypatch, "cleanup", "--all", "--execute")
    assert r.exit_code == 0, r.output
    c.post.assert_called_once_with("/v1/upkeep/cleanup", params={"dry_run": "false"})


def test_search_sync_all(monkeypatch):
    r, c = _invoke(monkeypatch, "search-sync", "--all", "--force")
    assert r.exit_code == 0, r.output
    c.post.assert_called_once_with("/v1/upkeep/search-sync?force=true")


def test_search_sync_has_no_library(monkeypatch):
    r, c = _invoke(monkeypatch, "search-sync", "--all", "--library", "Media")
    assert r.exit_code == 2
    c.post.assert_not_called()


def test_cleanup_dismissed_all(monkeypatch):
    r, c = _invoke(monkeypatch, "cleanup-dismissed", "--all")
    assert r.exit_code == 0, r.output
    c.post.assert_called_once_with("/v1/upkeep/cleanup-dismissed")
    assert "No empty dismissed people found." in r.output
