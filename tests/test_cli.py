"""CLI tests: config, library create/list. All use mocks; no real HTTP or DB."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.cli.main import app

runner = CliRunner()


@pytest.mark.fast
def test_config_set_and_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Set config, show config, assert values match."""
    config_file = tmp_path / "config.json"
    monkeypatch.setattr("src.cli.config._config_path", lambda: config_file)

    result_set = runner.invoke(
        app,
        ["config", "set", "--api-url", "http://test.example.com", "--api-key", "sk_test_xyz"],
    )
    assert result_set.exit_code == 0

    result_show = runner.invoke(app, ["config", "show"])
    assert result_show.exit_code == 0
    assert "http://test.example.com" in result_show.output
    assert "[set]" in result_show.output


@pytest.mark.fast
def test_library_create_prints_id() -> None:
    """Mock client.post; assert output contains lib_."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "library_id": "lib_01HXYZ",
        "name": "My Library",
        "root_path": "/photos",
        "scan_status": "idle",
    }
    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["library", "create", "My Library", "/photos"])

    assert result.exit_code == 0
    assert "lib_" in result.output
    assert "My Library" in result.output
    mock_client.post.assert_called_once()
    call_kw = mock_client.post.call_args[1]
    assert call_kw["json"] == {"name": "My Library", "root_path": "/photos"}


@pytest.mark.fast
def test_library_list_shows_table() -> None:
    """Mock client.get returning two libraries; assert both names appear in output."""
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {
            "library_id": "lib_01A",
            "name": "First Lib",
            "root_path": "/path/a",
            "scan_status": "idle",
            "last_scan_at": None,
        },
        {
            "library_id": "lib_01B",
            "name": "Second Lib",
            "root_path": "/path/b",
            "scan_status": "scanning",
            "last_scan_at": "2025-01-15T10:00:00",
        },
    ]
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["library", "list"])

    assert result.exit_code == 0
    assert "First Lib" in result.output
    assert "Second Lib" in result.output
    mock_client.get.assert_called_once_with("/v1/libraries")
