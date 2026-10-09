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
    result = _run(client, "archive", "add", "--library", "Media", "--folder", "Trips/Paris", input="y\n")
    assert result.exit_code == 0, result.output
    assert "Archive every clip under 'Trips/Paris' in Media?" in result.output
    client.post.assert_called_with("/v1/assets/archive", json={"library_id": "lib_1", "path": "Trips/Paris"})
    assert "Archived 1 clip." in result.output


def test_the_whole_library_is_an_empty_folder():
    client = _client(**{"/v1/assets/archive": {"archived": [], "skipped": []}})
    result = _run(client, "archive", "add", "--library", "lib_1", "--folder", "", "--yes")
    assert result.exit_code == 0, result.output
    client.post.assert_called_with("/v1/assets/archive", json={"library_id": "lib_1", "path": ""})


def test_archiving_a_folder_asks_first():
    client = _client()
    result = _run(client, "archive", "add", "--library", "Media", "--folder", "", input="n\n")
    assert result.exit_code == 0 and "Archive every clip in Media?" in result.output
    assert not [c for c in client.post.call_args_list if c.args[0] == "/v1/assets/archive"]


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


def test_trash_restore_says_which_went_back_to_the_archive():
    client = _client(**{"/v1/assets/restore": {"restored": ["a1", "a2"], "skipped": [], "to_archive": ["a2"]}})
    result = _run(client, "trash", "restore", "a1", "a2")
    assert "Restored 2 clips. 1 clip went back to the archive, where it was." in " ".join(result.output.split())


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


def test_deleting_missing_clips_says_how_many_and_asks():
    client = _client()
    asks = _answer(409, {"error": {"code": "confirm_delete_missing", "message": "3 clips",
                                   "details": {"count": 3, "listed_at": "2026-10-09T06:00:00+00:00"}}})
    client.raw.side_effect = [asks]
    result = _run(client, "archive", "delete-missing", "--library", "Media", input="n\n")
    assert result.exit_code == 0 and "Delete 3 clips whose files are missing in Media for good?" in result.output
    assert "Aborted" in result.output and client.raw.call_count == 1

    client.raw.reset_mock()
    client.raw.side_effect = [asks, _answer(200, {"deleted": 3})]
    result = _run(client, "archive", "delete-missing", "--library", "Media", "--folder", "licensed", input="y\n")
    assert result.exit_code == 0, result.output
    assert client.raw.call_args.args == ("DELETE", "/v1/archive/missing")
    assert client.raw.call_args.kwargs["json"] == {"library_id": "lib_1", "path": "licensed", "count": 3,
                                                   "missing_before": "2026-10-09T06:00:00+00:00",
                                                   "remove_from_projects": False}
    assert "Deleted 3 clips whose files were missing, for good." in result.output


def test_deleting_missing_clips_in_projects_asks_about_them_too():
    client = _client()
    client.raw.side_effect = [
        _answer(409, {"error": {"code": "confirm_delete_missing", "details": {"count": 1, "listed_at": "t"}}}),
        _answer(409, {"error": {"code": "in_projects", "details": {"assets_in_projects": 1, "projects": [
            {"project_id": "col_1", "name": "Promo", "asset_count": 1}]}}}),
        _answer(200, {"deleted": 1}),
    ]
    result = _run(client, "archive", "delete-missing", input="y\ny\n")
    assert result.exit_code == 0, result.output
    assert "Promo" in result.output
    assert client.raw.call_args.kwargs["json"] == {"count": 1, "missing_before": "t", "remove_from_projects": True}


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


def test_emptying_one_librarys_trash():
    client = _client()
    client.raw.return_value = _answer(200, {"deleted": 3})
    result = _run(client, "trash", "empty", "--all", "--library", "Media", "--folder", "x", input="y\n")
    assert result.exit_code == 0, result.output
    assert "every clip in the trash in Media under 'x'" in result.output
    assert client.raw.call_args.kwargs["json"] == {"library_id": "lib_1", "path": "x", "remove_from_projects": False}
    assert _run(client, "trash", "empty", "a1", "--library", "Media").exit_code == 2


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
    client.raw.return_value = _answer(200, {"trash_days": value})
    result = _run(client, "settings", "trash-days", arg)
    assert result.exit_code == 0, result.output
    client.raw.assert_called_once_with("PATCH", "/v1/tenant/settings", json={"trash_days": value, "confirm_purge": False})
    assert shown in result.output


@pytest.mark.parametrize("answer,calls", [("y", 2), ("n", 1)])
def test_fewer_trash_days_says_what_goes_and_asks(answer, calls):
    client = _client()
    shortened = _answer(409, {"error": {"code": "trash_days_shortened", "message": "m",
                                        "details": {"trash_days": 7, "clips": 12, "libraries": 1, "projects": 0}}})
    client.raw.side_effect = [shortened, _answer(200, {"trash_days": 7})]
    result = _run(client, "settings", "trash-days", "7", input=f"{answer}\n")
    assert result.exit_code == 0, result.output
    assert "12 clips, 1 library already in the trash longer" in " ".join(result.output.split())
    assert client.raw.call_count == calls
    if calls == 2:
        assert client.raw.call_args.kwargs["json"] == {"trash_days": 7, "confirm_purge": True}


@pytest.mark.parametrize("arg", ["0", "3651", "-1", "2.5", "thirty", "٣"])
def test_settings_trash_days_refuses_nonsense(arg):
    client = _client()
    result = _run(client, "settings", "trash-days", "--", arg)
    assert result.exit_code == 2, result.output
    client.raw.assert_not_called()


def test_settings_show_says_how_long_the_trash_keeps_things():
    client = _client(**{"/v1/tenant/settings": {"trash_days": 30}})
    assert "Trash: deleted for good after 30 days" in _run(client, "settings", "show").output
    client = _client(**{"/v1/tenant/settings": {"trash_days": None}})
    assert "Trash: emptied by hand only" in _run(client, "settings", "show").output
