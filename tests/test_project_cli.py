"""CLI project lifecycle: list active/archived/all, archive, restore."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.client.cli.main import app

pytestmark = pytest.mark.fast
runner = CliRunner()


def _run(client: MagicMock, *args: str, input: str | None = None):
    with patch("src.client.cli.commands.projects.LumiverbClient", return_value=client):
        return runner.invoke(app, ["project", *args], input=input)


def _client() -> MagicMock:
    client = MagicMock()
    client.get.return_value.json.return_value = {"items": []}
    client.patch.return_value.json.return_value = {"project_id": "prj_1", "name": "Job", "status": "archived"}
    client.raw.return_value.status_code = 404  # not in the trash
    return client


@pytest.mark.parametrize(
    ("flags", "params"),
    [([], None), (["--archived"], {"status": "archived"}), (["--all"], {"status": "all"})],
)
def test_list_by_status(flags: list[str], params: dict | None) -> None:
    client = _client()

    result = _run(client, "list", *flags)

    assert result.exit_code == 0, result.output
    assert client.get.call_args[0][0] == "/v1/projects"
    assert client.get.call_args[1].get("params") == params


def test_list_archived_and_all_are_exclusive() -> None:
    result = _run(_client(), "list", "--archived", "--all")

    assert result.exit_code == 1


@pytest.mark.parametrize(("command", "status"), [("archive", "archived"), ("restore", "active")])
def test_archive_and_restore(command: str, status: str) -> None:
    client = _client()

    result = _run(client, command, "--id", "prj_1")

    assert result.exit_code == 0, result.output
    client.patch.assert_called_once_with("/v1/projects/prj_1", json={"status": status})


def _export_client(stills: str = "0") -> MagicMock:
    client = _client()
    client.get.return_value.content = b"<xmeml/>"
    client.get.return_value.headers = {
        "content-disposition": 'attachment; filename="Customer Video 123.xml"',
        "x-lumiverb-stills": stills,
    }
    return client


def test_export_writes_the_file(tmp_path) -> None:
    import os

    client = _export_client()
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = _run(client, "export", "--id", "prj_1", "--format", "fcp7")
    finally:
        os.chdir(cwd)

    assert result.exit_code == 0, result.output
    client.get.assert_called_once_with("/v1/projects/prj_1/export", params={"format": "fcp7"})
    assert (tmp_path / "Customer Video 123.xml").read_bytes() == b"<xmeml/>"


def test_export_with_output_says_photos_are_stills(tmp_path) -> None:
    # Photos export as stills; there's no media location to give (Robert, Oct 9).
    client = _export_client(stills="2")
    out = tmp_path / "bin.fcpxml"

    result = _run(client, "export", "--id", "prj_1", "--format", "fcpxml", "--output", str(out))

    assert result.exit_code == 0, result.output
    client.get.assert_called_once_with("/v1/projects/prj_1/export", params={"format": "fcpxml"})
    assert out.read_bytes() == b"<xmeml/>"
    assert "2 photos are in it as stills." in result.output
    assert _run(client, "export", "--id", "prj_1", "--format", "fcpxml", "--prefix", "/x").exit_code != 0


def test_export_rejects_unknown_format() -> None:
    client = _export_client()

    result = _run(client, "export", "--id", "prj_1", "--format", "edl")

    assert result.exit_code != 0
    client.get.assert_not_called()


def test_export_uses_the_real_name_and_reports_what_was_left_out(tmp_path) -> None:
    import os

    client = _export_client()
    client.get.return_value.headers = {
        "content-disposition": (
            "attachment; filename=\"project-export.xml\"; filename*=UTF-8''%E6%9D%B1%E4%BA%AC.xml"
        ),
        "x-lumiverb-stills": "0",
        "x-lumiverb-skipped-no-duration": "2",
        "x-lumiverb-unprobed": "3",
    }
    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = _run(client, "export", "--id", "prj_1", "--format", "fcp7")
    finally:
        os.chdir(cwd)

    assert result.exit_code == 0, result.output
    assert (tmp_path / "東京.xml").exists()
    assert "2 videos with no known length" in result.output
    assert "3 videos haven't been probed" in result.output


# ---------------------------------------------------------------------------
# Trash: delete -> trash -> restore | delete forever
# ---------------------------------------------------------------------------


def _response(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    return r


def _trash_client(*, in_trash: bool = True, trashed_clips: int = 0) -> MagicMock:
    """A server whose project restore needs a choice when clips are in the trash."""
    client = _client()

    def raw(method: str, path: str, **kwargs: object) -> MagicMock:
        if path.endswith("/restore"):
            if not in_trash:
                return _response(404)
            choice = (kwargs.get("json") or {}).get("with_clips")
            if trashed_clips and choice is None:
                return _response(409, {"error": {
                    "code": "clips_in_trash", "message": "choose",
                    "details": {"trashed_clips": trashed_clips, "missing_clips": 1},
                }})
            restored = trashed_clips if choice else 0
            return _response(200, {"restored_clips": restored, "trashed_clips": trashed_clips - restored,
                                   "missing_clips": 1})
        return _response(204)

    client.raw.side_effect = raw
    client.get.return_value.json.return_value = {
        "items": [{"project_id": "prj_1", "name": "Job", "asset_count": 3}],
    }
    client.post.return_value.json.return_value = {"restored": 2, "missing": 1, "deleted": 1}
    return client


def _restore_calls(client: MagicMock) -> list:
    return [c for c in client.raw.call_args_list if c.args[1].endswith("/restore")]


def test_list_trashed() -> None:
    client = _client()
    result = _run(client, "list", "--trashed")
    assert result.exit_code == 0, result.output
    assert client.get.call_args[1].get("params") == {"status": "trashed"}


def test_list_trashed_excludes_the_other_views() -> None:
    assert _run(_client(), "list", "--trashed", "--archived").exit_code == 1


def test_delete_moves_to_the_trash_without_asking() -> None:
    client = _trash_client()
    result = _run(client, "delete", "--id", "prj_1")
    assert result.exit_code == 0, result.output
    client.raw.assert_called_once_with("DELETE", "/v1/projects/prj_1")
    assert "trash" in result.output and "project restore --id prj_1" in result.output


def test_restore_takes_a_project_out_of_the_trash() -> None:
    client = _trash_client(in_trash=True)
    result = _run(client, "restore", "--id", "prj_1")
    assert result.exit_code == 0, result.output
    assert _restore_calls(client)[0].kwargs["json"] == {}
    client.patch.assert_not_called()
    assert "out of the trash" in result.output


def test_restore_unarchives_a_project_that_isnt_in_the_trash() -> None:
    client = _trash_client(in_trash=False)
    result = _run(client, "restore", "--id", "prj_1")
    assert result.exit_code == 0, result.output
    client.patch.assert_called_once_with("/v1/projects/prj_1", json={"status": "active"})


@pytest.mark.parametrize(("answer", "choice", "restored"), [("y\n", True, "Restored 2 clips"), ("n\n", False, "Left 2 clips")])
def test_restore_asks_about_trashed_clips(answer: str, choice: bool, restored: str) -> None:
    client = _trash_client(trashed_clips=2)
    result = _run(client, "restore", "--id", "prj_1", input=answer)
    assert result.exit_code == 0, result.output
    assert "2 clips in this project are in the trash" in result.output
    assert _restore_calls(client)[-1].kwargs["json"] == {"with_clips": choice}
    assert restored in result.output


@pytest.mark.parametrize(("flag", "choice"), [("--with-clips", True), ("--without-clips", False)])
def test_restore_with_a_flag_doesnt_ask(flag: str, choice: bool) -> None:
    client = _trash_client(trashed_clips=2)
    result = _run(client, "restore", "--id", "prj_1", flag)
    assert result.exit_code == 0, result.output
    assert len(_restore_calls(client)) == 1
    assert _restore_calls(client)[0].kwargs["json"] == {"with_clips": choice}
    assert "1 clip is missing from disk" in result.output


def test_restore_clips() -> None:
    client = _trash_client()
    result = _run(client, "restore-clips", "--id", "prj_1")
    assert result.exit_code == 0, result.output
    client.post.assert_called_once_with("/v1/projects/prj_1/restore-clips")


def test_empty_trash_asks_first() -> None:
    client = _trash_client()
    result = _run(client, "empty-trash", input="n\n")
    assert result.exit_code == 0, result.output
    client.post.assert_not_called()
    result = _run(client, "empty-trash", input="y\n")
    # Exactly the projects it listed, not whatever is in the trash by now.
    client.post.assert_called_once_with("/v1/projects/empty-trash", json={"project_ids": ["prj_1"]})


def test_empty_trash_named_projects_without_asking() -> None:
    client = _trash_client()
    result = _run(client, "empty-trash", "--id", "prj_1", "--yes")
    assert result.exit_code == 0, result.output
    client.post.assert_called_once_with("/v1/projects/empty-trash", json={"project_ids": ["prj_1"]})


def test_empty_trash_when_empty() -> None:
    client = _trash_client()
    client.get.return_value.json.return_value = {"items": []}
    result = _run(client, "empty-trash", "--yes")
    assert "The trash is empty" in result.output
    client.post.assert_not_called()


def test_emptying_the_library_trash_says_which_projects_lose_clips() -> None:
    client = MagicMock()
    client.get.return_value.json.return_value = [
        {"library_id": "lib_1", "name": "Old card", "status": "trashed"},
    ]

    def post(path: str, **kwargs: object) -> MagicMock:
        if path == "/v1/assets/project-usage":
            assert kwargs["json"] == {"library_ids": ["lib_1"]}
            return _response(200, {
                "assets_in_projects": 4,
                "projects": [{"project_id": "prj_1", "name": "Customer Video", "status": "active",
                              "in_trash": False, "clips": 4}],
                "other_projects": 1,
            })
        return _response(200, {"deleted": 1})

    client.post.side_effect = post
    with patch("src.client.cli.main.LumiverbClient", return_value=client):
        result = runner.invoke(app, ["library", "empty-trash", "--all"], input="n\n")
    assert result.exit_code == 0, result.output
    assert "4 clips" in result.output and "2 projects" in result.output and "Customer Video" in result.output
    assert not [c for c in client.post.call_args_list if c.args[0] == "/v1/libraries/empty-trash"]

    with patch("src.client.cli.main.LumiverbClient", return_value=client):
        result = runner.invoke(app, ["library", "empty-trash", "--all"], input="y\n")
    assert result.exit_code == 0, result.output
    empty = [c for c in client.post.call_args_list if c.args[0] == "/v1/libraries/empty-trash"]
    assert empty[-1].kwargs["json"] == {"library_ids": ["lib_1"], "remove_from_projects": True}


def test_restore_says_it_for_one_clip() -> None:
    client = _trash_client(trashed_clips=1)
    result = _run(client, "restore", "--id", "prj_1", input="y\n")
    assert result.exit_code == 0, result.output
    assert "1 clip in this project is in the trash. Restoring it brings it back" in result.output
    assert "Restore it too?" in result.output


def test_emptying_the_library_trash_reads_right_for_one() -> None:
    client = MagicMock()
    client.get.return_value.json.return_value = [{"library_id": "lib_1", "name": "Old card", "status": "trashed"}]

    def post(path: str, **kwargs: object) -> MagicMock:
        if path == "/v1/assets/project-usage":
            return _response(200, {
                "assets_in_projects": 1,
                "projects": [{"project_id": "prj_1", "name": "Reel", "status": "active", "in_trash": False, "clips": 1}],
                "other_projects": 0,
            })
        return _response(200, {"deleted": 1})

    client.post.side_effect = post
    with patch("src.client.cli.main.LumiverbClient", return_value=client):
        result = runner.invoke(app, ["library", "empty-trash", "--all"], input="y\n")
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "1 clip from this library is in 1 project; deleting it for good removes it from that project" in out
    assert "Permanently delete 1 library and all its assets?" in out
    assert "[y/N] [y/N]" not in out
    assert "Deleted 1 library." in out


def test_deleting_a_library_asks_once() -> None:
    client = MagicMock()
    client.get.side_effect = lambda path, **kw: MagicMock(json=MagicMock(
        return_value=[{"library_id": "lib_1", "name": "Old card"}] if path == "/v1/libraries" else {"total": 0}))
    client.raw.return_value.status_code = 204
    with patch("src.client.cli.main.LumiverbClient", return_value=client):
        result = runner.invoke(app, ["library", "delete", "--name", "Old card"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "[y/N] [y/N]" not in result.output


def test_export_reports_trashed_and_missing_clips(tmp_path) -> None:
    client = _export_client()
    client.get.return_value.headers = {
        "content-disposition": 'attachment; filename="Job.xml"',
        "x-lumiverb-skipped-trashed": "2",
        "x-lumiverb-skipped-missing": "1",
        "x-lumiverb-skipped-library-trashed": "3",
        "x-lumiverb-skipped-archived": "4",
    }
    result = _run(client, "export", "--id", "prj_1", "--format", "fcp7", "--output", str(tmp_path / "Job.xml"))
    assert result.exit_code == 0, result.output
    out = " ".join(result.output.split())
    assert "2 clips in the trash weren't included" in out
    assert "1 clip missing from disk wasn't included" in out
    assert "3 clips in a deleted library weren't included; restoring the library brings them back" in out
    assert "4 archived clips weren't included" in out and "lumiverb archive restore" in out
