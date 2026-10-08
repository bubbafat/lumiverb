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
