"""CLI project lifecycle: list active/archived/all, archive, restore."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.client.cli.main import app

pytestmark = pytest.mark.fast
runner = CliRunner()


def _run(client: MagicMock, *args: str):
    with patch("src.client.cli.commands.projects.LumiverbClient", return_value=client):
        return runner.invoke(app, ["project", *args])


def _client() -> MagicMock:
    client = MagicMock()
    client.get.return_value.json.return_value = {"items": []}
    client.patch.return_value.json.return_value = {"project_id": "prj_1", "name": "Job", "status": "archived"}
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


def _export_client(skipped: str = "0") -> MagicMock:
    client = _client()
    client.get.return_value.content = b"<xmeml/>"
    client.get.return_value.headers = {
        "content-disposition": 'attachment; filename="Customer Video 123.xml"',
        "x-lumiverb-skipped-stills": skipped,
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


def test_export_with_prefix_and_output(tmp_path) -> None:
    client = _export_client(skipped="2")
    out = tmp_path / "bin.fcpxml"

    result = _run(
        client, "export", "--id", "prj_1", "--format", "fcpxml",
        "--prefix", "/Volumes/Travel SSD", "--output", str(out),
    )

    assert result.exit_code == 0, result.output
    client.get.assert_called_once_with(
        "/v1/projects/prj_1/export",
        params={"format": "fcpxml", "prefix": "/Volumes/Travel SSD"},
    )
    assert out.read_bytes() == b"<xmeml/>"
    assert "2 photos" in result.output


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
        "x-lumiverb-skipped-stills": "0",
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
