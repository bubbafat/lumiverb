"""`lumiverb archive`, `lumiverb trash`, `library restore` and `settings trash-days`.

The CLI mirrors the web's Archive and Trash views (Robert's model, Oct 8), and
asks the same questions the API requires: clips in projects before they go
to the trash, and a yes before anything is deleted for good.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.fast

runner = CliRunner()
LIBS = [{"library_id": "lib_1", "name": "Media", "status": "active"},
        {"library_id": "lib_old", "name": "Old", "status": "trashed"}]


def _app():
    return importlib.import_module("src.client.cli.main").app


def _client(**json_by_path) -> MagicMock:
    """GET and POST answer JSON by path; /v1/libraries lists LIBS."""
    client = MagicMock()

    def reply(path, **_):
        r = MagicMock(status_code=200)
        r.json.return_value = LIBS if path == "/v1/libraries" else json_by_path.get(path, {})
        return r

    client.get.side_effect = reply
    client.post.side_effect = reply
    client.patch.side_effect = lambda path, json: MagicMock(json=MagicMock(return_value=json))
    return client


def _run(client: MagicMock, *args: str, input: str | None = None):
    with patch("src.client.cli.commands.archive.LumiverbClient", return_value=client), \
         patch("src.client.cli.commands.trash.LumiverbClient", return_value=client), \
         patch("src.client.cli.commands.settings.LumiverbClient", return_value=client), \
         patch("src.client.cli.main.LumiverbClient", return_value=client):
        return runner.invoke(_app(), list(args), input=input)


def _answer(status: int, body: dict) -> MagicMock:
    r = MagicMock(status_code=status)
    r.json.return_value = body
    r.text = str(body)
    return r


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


def test_archive_clips_by_id():
    client = _client(**{"/v1/assets/archive": {"archived": ["a1", "a2"], "skipped": ["a3"]}})
    result = _run(client, "archive", "add", "a1", "a2", "a3")
    assert result.exit_code == 0, result.output
    client.post.assert_called_with("/v1/assets/archive", json={"asset_ids": ["a1", "a2", "a3"]})
    assert "Archived 2 clips." in result.output and "Skipped 1 clip" in result.output


def test_archive_a_folder_by_library_name():
    client = _client(**{"/v1/assets/archive": {"archived": ["a1"], "skipped": []}})
    result = _run(client, "archive", "add", "--library", "Media", "--folder", "Trips/Paris")
    assert result.exit_code == 0, result.output
    client.post.assert_called_with("/v1/assets/archive", json={"library_id": "lib_1", "path": "Trips/Paris"})
    assert "Archived 1 clip." in result.output


def test_the_whole_library_is_an_empty_folder():
    client = _client(**{"/v1/assets/archive": {"archived": [], "skipped": []}})
    result = _run(client, "archive", "add", "--library", "lib_1", "--folder", "")
    assert result.exit_code == 0, result.output
    client.post.assert_called_with("/v1/assets/archive", json={"library_id": "lib_1", "path": ""})


@pytest.mark.parametrize("args", [
    (),                                            # nothing picked
    ("a1", "--library", "Media", "--folder", "x"),  # both ways
    ("--folder", "x"),                             # folder without its library
    ("--library", "Media"),                        # library without a folder
])
def test_archive_needs_exactly_one_way_to_pick_clips(args):
    client = _client()
    result = _run(client, "archive", "add", *args)
    assert result.exit_code == 2, result.output
    client.post.assert_not_called()


def test_an_unknown_library_stops_before_anything_changes():
    client = _client()
    result = _run(client, "archive", "add", "--library", "Nope", "--folder", "x")
    assert result.exit_code == 1
    assert "Library not found: Nope" in result.output
    client.post.assert_not_called()


def test_unarchive_says_what_it_skipped():
    client = _client(**{"/v1/assets/unarchive": {"unarchived": ["a1"], "skipped": ["gone"]}})
    result = _run(client, "archive", "restore", "a1", "gone")
    assert result.exit_code == 0, result.output
    client.post.assert_called_with("/v1/assets/unarchive", json={"asset_ids": ["a1", "gone"]})
    assert "Unarchived 1 clip." in result.output and "missing file comes back" in result.output


def test_archive_list_marks_missing_files():
    page = {"items": [
        {"asset_id": "a1", "library_name": "Media", "rel_path": "x/a.mov", "archived_at": "2026-10-08T12:00:00+00:00",
         "file_missing": True},
        {"asset_id": "a2", "library_name": "Media", "rel_path": "x/b.mov", "archived_at": "2026-10-08T12:00:00+00:00",
         "file_missing": False},
    ], "total": 5}
    client = _client(**{"/v1/archive": page})
    result = _run(client, "archive", "list", "--library", "Media", "--folder", "x", "--missing")
    assert result.exit_code == 0, result.output
    client.get.assert_called_with("/v1/archive", params={"kind": "missing", "limit": 100, "library_id": "lib_1",
                                                         "path": "x"})
    assert "file missing" in result.output and "Showing 2 of 5 clips." in result.output


# ---------------------------------------------------------------------------
# trash
# ---------------------------------------------------------------------------


def test_trash_add_asks_about_projects_then_goes_ahead():
    client = _client()
    usage = {"assets_in_projects": 1, "projects": [{"name": "Promo", "clips": 1, "status": "active"}],
             "other_projects": 0}
    client.raw.side_effect = [
        _answer(409, {"error": {"code": "in_projects", "message": "m", "details": usage}}),
        _answer(200, {"trashed": ["a1"], "not_found": []}),
    ]
    result = _run(client, "trash", "add", "a1", input="y\n")
    assert result.exit_code == 0, result.output
    assert "Promo: 1" in result.output and "Moved 1 clip to the trash." in result.output
    assert client.raw.call_args_list[-1].kwargs["json"] == {"asset_ids": ["a1"], "reason": "user",
                                                            "remove_from_projects": True}


def test_trash_add_with_yes_but_no_flag_stops_at_projects():
    client = _client()
    client.raw.return_value = _answer(409, {"error": {"code": "in_projects", "message": "m",
                                                      "details": {"assets_in_projects": 1}}})
    result = _run(client, "trash", "add", "a1", "--yes")
    assert result.exit_code == 2 and "--remove-from-projects" in result.output
    assert client.raw.call_count == 1


def test_trash_restore():
    client = _client(**{"/v1/assets/restore": {"restored": ["a1"], "skipped": ["a2"]}})
    result = _run(client, "trash", "restore", "a1", "a2")
    assert result.exit_code == 0, result.output
    client.post.assert_called_with("/v1/assets/restore", json={"asset_ids": ["a1", "a2"]})
    assert "Restored 1 clip." in result.output and "Skipped 1 clip" in result.output


def test_trash_list_shows_when_each_goes():
    page = {"items": [{"asset_id": "a1", "library_name": "Media", "rel_path": "x/a.mov",
                       "trashed_at": "2026-10-01T12:00:00+00:00", "expires_at": "2026-10-31T12:00:00+00:00"}],
            "total": 1, "trash_days": 30}
    client = _client(**{"/v1/trash": page})
    result = _run(client, "trash", "list")
    assert result.exit_code == 0, result.output
    assert "2026-10-31" in result.output


def test_trash_list_says_when_the_trash_is_emptied_by_hand():
    page = {"items": [{"asset_id": "a1", "library_name": "Media", "rel_path": "x/a.mov",
                       "trashed_at": "2026-10-01T12:00:00+00:00", "expires_at": None}],
            "total": 1, "trash_days": None}
    result = _run(_client(**{"/v1/trash": page}), "trash", "list")
    assert "when emptied" in result.output and "emptied by hand only" in result.output


def test_emptying_asks_first_and_names_what_goes():
    client = _client()
    result = _run(client, "trash", "empty", "a1", "a2", input="n\n")
    assert result.exit_code == 0 and "Aborted" in result.output
    client.raw.assert_not_called()

    client.raw.return_value = _answer(200, {"deleted": 2})
    result = _run(client, "trash", "empty", "a1", "a2", input="y\n")
    assert result.exit_code == 0, result.output
    assert client.raw.call_args.args == ("DELETE", "/v1/trash/empty")
    assert client.raw.call_args.kwargs["json"] == {"asset_ids": ["a1", "a2"], "remove_from_projects": False}
    assert "Deleted 2 clips for good." in result.output


def test_emptying_everything_needs_all():
    client = _client()
    assert _run(client, "trash", "empty").exit_code == 2
    assert _run(client, "trash", "empty", "a1", "--all").exit_code == 2
    client.raw.return_value = _answer(200, {"deleted": 7})
    result = _run(client, "trash", "empty", "--all", "--yes")
    assert result.exit_code == 0, result.output
    assert client.raw.call_args.kwargs["json"] == {"remove_from_projects": False}


# ---------------------------------------------------------------------------
# library restore, settings trash-days
# ---------------------------------------------------------------------------


def test_library_restore_finds_it_in_the_trash():
    client = _client()
    result = _run(client, "library", "restore", "--name", "Old")
    assert result.exit_code == 0, result.output
    client.post.assert_called_with("/v1/libraries/lib_old/restore")
    assert "private" in result.output


def test_library_restore_of_one_not_in_the_trash_says_so():
    client = _client()
    result = _run(client, "library", "restore", "--name", "Media")
    assert result.exit_code == 1 and "in the trash" in result.output
    client.post.assert_not_called()


def test_library_empty_trash_of_one_library():
    client = _client(**{"/v1/assets/project-usage": {"assets_in_projects": 0}, "/v1/libraries/empty-trash": {"deleted": 1}})
    result = _run(client, "library", "empty-trash", "--name", "Old", input="y\n")
    assert result.exit_code == 0, result.output
    client.post.assert_called_with("/v1/libraries/empty-trash", json={"library_ids": ["lib_old"],
                                                                      "remove_from_projects": False})


@pytest.mark.parametrize("arg,value,shown", [("7", 7, "after 7 days"), ("1", 1, "after 1 day"),
                                             ("off", None, "emptied by hand only"), ("OFF", None, "by hand")])
def test_settings_trash_days(arg, value, shown):
    client = _client()
    result = _run(client, "settings", "trash-days", arg)
    assert result.exit_code == 0, result.output
    client.patch.assert_called_once_with("/v1/tenant/settings", json={"trash_days": value})
    assert shown in result.output


@pytest.mark.parametrize("arg", ["0", "3651", "-1", "2.5", "thirty", "٣"])
def test_settings_trash_days_refuses_nonsense(arg):
    client = _client()
    result = _run(client, "settings", "trash-days", "--", arg)
    assert result.exit_code == 2, result.output
    client.patch.assert_not_called()


def test_settings_show_says_how_long_the_trash_keeps_things():
    client = _client(**{"/v1/tenant/settings": {"trash_days": 30}})
    assert "Trash: deleted for good after 30 days" in _run(client, "settings", "show").output
    client = _client(**{"/v1/tenant/settings": {"trash_days": None}})
    assert "Trash: emptied by hand only" in _run(client, "settings", "show").output
