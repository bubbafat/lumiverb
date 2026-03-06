"""CLI tests: config, library create/list, scan. All use mocks; no real HTTP or DB."""

import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.cli.main import app
from src.cli.scanner import ScanResult

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


@pytest.mark.fast
def test_scan_aborts_if_root_unreachable() -> None:
    """Mock scan_library to return ScanResult(status='aborted'); assert output indicates abort and exit code 1."""
    mock_client = MagicMock()
    mock_client.get.return_value.json.return_value = [
        {"library_id": "lib_1", "name": "UnreachableLib", "root_path": "/nonexistent"}
    ]
    aborted = ScanResult(
        scan_id="",
        files_discovered=0,
        files_added=0,
        files_updated=0,
        files_skipped=0,
        files_missing=0,
        status="aborted",
    )

    with patch("src.cli.main.LumiverbClient", return_value=mock_client), patch(
        "src.cli.main.scan_library", return_value=aborted
    ):
        result = runner.invoke(app, ["scan", "--library", "UnreachableLib"])

    assert result.exit_code == 1
    assert "Discovered" in result.output
    assert "0" in result.output


@pytest.mark.fast
def test_scan_registers_signal_handlers(tmp_path: Path) -> None:
    """Patch signal.signal; invoke scan so real scan_library runs and completes; assert SIGINT/SIGTERM registered and restored."""
    def _json(d):
        m = MagicMock()
        m.json.return_value = d
        return m

    mock_client = MagicMock()
    mock_client.get.side_effect = [
        _json([{"library_id": "lib_1", "name": "SigLib", "root_path": str(tmp_path)}]),
        _json([]),  # no running scans
    ]
    mock_client.post.side_effect = [
        _json({"scan_id": "scan_1"}),
        _json({"files_missing": 0}),
        _json({"enqueued": 0}),  # enqueue after complete
    ]
    with patch("src.cli.main.LumiverbClient", return_value=mock_client), patch(
        "src.cli.scanner.signal.signal"
    ) as mock_signal:
        mock_signal.side_effect = [
            MagicMock(),
            MagicMock(),
            None,
            None,
        ]  # old handlers for register, then restore calls
        result = runner.invoke(app, ["scan", "--library", "SigLib", "--force"])
    assert result.exit_code == 0
    calls = mock_signal.call_args_list
    assert len(calls) >= 4, "expected 2 registrations + 2 restores"
    reg_sigint = next((c for c in calls if c[0][0] == signal.SIGINT), None)
    reg_sigterm = next((c for c in calls if c[0][0] == signal.SIGTERM), None)
    assert reg_sigint is not None, "SIGINT handler should be registered"
    assert reg_sigterm is not None, "SIGTERM handler should be registered"
    restore_calls = [c for c in calls if len(c[0]) == 2 and c[0][1] is not None]
    assert any(c[0][0] == signal.SIGINT for c in restore_calls), "SIGINT should be restored"
    assert any(c[0][0] == signal.SIGTERM for c in restore_calls), "SIGTERM should be restored"


@pytest.mark.fast
def test_scan_shows_conflict_warning(tmp_path: Path) -> None:
    """Mock client GET /v1/scans/running to return one running scan; patch input to return 'n'; assert warning and abort."""
    libs = [
        {
            "library_id": "lib_1",
            "name": "ConflictLib",
            "root_path": str(tmp_path),
        }
    ]
    running = [
        {"scan_id": "scan_1", "library_id": "lib_1", "started_at": "2025-01-01T00:00:00", "worker_id": None}
    ]
    mock_client = MagicMock()
    mock_client.get.side_effect = [
        MagicMock(json=lambda: libs),
        MagicMock(json=lambda: running),
    ]

    with patch("src.cli.main.LumiverbClient", return_value=mock_client), patch(
        "src.cli.scanner.input", return_value="n"
    ):
        result = runner.invoke(app, ["scan", "--library", "ConflictLib"])

    assert result.exit_code == 1
    assert "already running" in result.output or "scan_1" in result.output


@pytest.mark.fast
def test_scan_force_skips_warning(tmp_path: Path) -> None:
    """Pass --force; assert input() never called."""
    def _json(d):
        m = MagicMock()
        m.json.return_value = d
        return m

    libs = [{"library_id": "lib_1", "name": "ForceLib", "root_path": str(tmp_path)}]
    running = [{"scan_id": "scan_1", "library_id": "lib_1", "started_at": "2025-01-01T00:00:00", "worker_id": None}]
    mock_client = MagicMock()
    mock_client.get.side_effect = [_json(libs), _json(running)]
    mock_client.post.side_effect = [_json({"scan_id": "scan_1"}), _json({"files_missing": 0})]
    mock_input = MagicMock()

    with patch("src.cli.main.LumiverbClient", return_value=mock_client), patch(
        "src.cli.scanner.input", mock_input
    ):
        runner.invoke(app, ["scan", "--library", "ForceLib", "--force"])

    mock_input.assert_not_called()


@pytest.mark.fast
def test_worker_proxy_command_exists() -> None:
    """Worker proxy command exists and shows --once and --concurrency."""
    result = runner.invoke(app, ["worker", "proxy", "--help"])
    assert result.exit_code == 0
    assert "--once" in result.output
    assert "--concurrency" in result.output


@pytest.mark.fast
def test_scan_prints_summary() -> None:
    """Mock scan_library to return complete ScanResult with known counts; assert all counts in output."""
    mock_client = MagicMock()
    mock_client.get.return_value.json.return_value = [
        {"library_id": "lib_1", "name": "SummaryLib", "root_path": "/path"}
    ]
    complete = ScanResult(
        scan_id="scan_123",
        files_discovered=10,
        files_added=3,
        files_updated=2,
        files_skipped=5,
        files_missing=0,
        status="complete",
    )

    with patch("src.cli.main.LumiverbClient", return_value=mock_client), patch(
        "src.cli.main.scan_library", return_value=complete
    ):
        result = runner.invoke(app, ["scan", "--library", "SummaryLib"])

    assert result.exit_code == 0
    assert "10" in result.output
    assert "3" in result.output
    assert "2" in result.output
    assert "5" in result.output
    assert "0" in result.output
    assert "Discovered" in result.output
    assert "Added" in result.output
