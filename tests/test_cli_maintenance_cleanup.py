"""`lumiverb maintenance cleanup` says when the server skipped it because the
Upkeep switch is paused, so it doesn't read as "nothing to clean up" (Robert, Oct 9)."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.fast

RESULT = {"orphan_files": 0, "orphan_folders": [], "bytes_freed": 0, "skipped_libraries": 0,
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


def test_a_cleanup_lists_the_folders_it_kept(monkeypatch):
    folder = {"folder_id": "lib_gone", "path": "ten_1/lib_gone", "bytes": 2_000_000, "newest_mtime": "2026-10-01"}
    out = _run(monkeypatch, {**RESULT, "orphan_folders": [folder]})
    assert "Kept 1 folder(s)" in out and "lib_gone 2.0 MB" in out and "remove-orphan-folders" in out


@pytest.mark.parametrize("args", [[], ["lib_a", "--all"]])
def test_remove_orphan_folders_needs_ids_or_all(monkeypatch, args):
    r, c = _invoke(monkeypatch, "remove-orphan-folders", *args)
    assert r.exit_code == 2, r.output
    c.post.assert_not_called()


def test_remove_orphan_folders_sends_what_is_said(monkeypatch):
    r, c = _invoke(monkeypatch, "remove-orphan-folders", "lib_a", "lib_b", "--dry-run")
    assert r.exit_code == 0, r.output
    c.post.assert_called_once_with("/v1/upkeep/remove-orphan-folders",
                                   json={"folder_ids": ["lib_a", "lib_b"], "dry_run": True})
    r, c = _invoke(monkeypatch, "remove-orphan-folders", "--all")
    assert r.exit_code == 0, r.output
    c.post.assert_called_once_with("/v1/upkeep/remove-orphan-folders", json={"all": True, "dry_run": False})


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
    c.post.assert_called_once_with("/v1/upkeep/cleanup", params={"dry_run": "false", "all": "true"})


def test_search_sync_all(monkeypatch):
    r, c = _invoke(monkeypatch, "search-sync", "--all", "--force")
    assert r.exit_code == 0, r.output
    c.post.assert_called_once_with("/v1/upkeep/search-sync?force=true")


def test_search_sync_has_no_library(monkeypatch):
    r, c = _invoke(monkeypatch, "search-sync", "--all", "--library", "Media")
    assert r.exit_code == 2
    c.post.assert_not_called()


# What POST /v1/upkeep/search-sync answers (routers/upkeep.py SearchSyncResult).
SYNCED = {"synced": 0, "failed": 0, "transcripts_synced": 0, "transcripts_failed": 0}


def _syncs(monkeypatch, *answers: dict, force: bool = False):
    """`search-sync --all` against a server answering each call in turn: (its output, the client)."""
    from src.client.cli.main import app

    mod = importlib.import_module("src.client.cli.commands.maintenance")
    c = MagicMock()
    c.post.return_value.json.side_effect = list(answers)
    monkeypatch.setattr(mod, "LumiverbClient", lambda: c)
    r = CliRunner().invoke(app, ["maintenance", "search-sync", "--all", *(["--force"] if force else [])])
    assert r.exit_code == 0, r.output
    return " ".join(r.output.split()), c


def test_search_sync_goes_on_until_a_call_writes_nothing(monkeypatch):
    out, c = _syncs(monkeypatch, {**SYNCED, "synced": 1000, "transcripts_synced": 3}, {**SYNCED, "synced": 200},
                    SYNCED, force=True)
    # Everything again once (force clears the sync times), then what's left.
    assert [call.args[0] for call in c.post.call_args_list] == [
        "/v1/upkeep/search-sync?force=true", "/v1/upkeep/search-sync", "/v1/upkeep/search-sync"]
    assert "Batch 1: 1000 clips, 3 transcripts synced" in out and "Batch 2: 200 clips, 0 transcripts synced" in out
    assert "Clips synced 1200 Transcripts synced 3 Failed 0" in out


def test_search_sync_of_transcripts_alone_goes_on(monkeypatch):
    out, c = _syncs(monkeypatch, {**SYNCED, "transcripts_synced": 40}, SYNCED)
    assert c.post.call_count == 2
    assert "Clips synced 0 Transcripts synced 40 Failed 0" in out


@pytest.mark.parametrize("failed", ["failed", "transcripts_failed"])
def test_search_sync_stops_when_something_failed(monkeypatch, failed):
    """Asked again at once it would fail the same way: the next upkeep tries again."""
    out, c = _syncs(monkeypatch, {**SYNCED, "synced": 10, failed: 1}, {**SYNCED, "synced": 10})
    assert c.post.call_count == 1
    assert "Clips synced 10 Transcripts synced 0 Failed 1" in out


def test_cleanup_dismissed_all(monkeypatch):
    r, c = _invoke(monkeypatch, "cleanup-dismissed", "--all")
    assert r.exit_code == 0, r.output
    c.post.assert_called_once_with("/v1/upkeep/cleanup-dismissed")
    assert "No empty dismissed people found." in r.output
