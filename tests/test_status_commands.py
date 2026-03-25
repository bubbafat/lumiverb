"""Fast tests for lumiverb status and failures CLI commands."""

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.cli.main import app

runner = CliRunner()


def _mock_client_for_status(*, pipeline_response: dict) -> MagicMock:
    """Build a mock LumiverbClient for status command."""
    mock_client = MagicMock()

    def _get_side_effect(path: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        if path == "/v1/libraries":
            resp.json.return_value = [{"library_id": "lib_01", "name": "Test", "root_path": "/photos"}]
        elif path == "/v1/pipeline/status":
            resp.json.return_value = pipeline_response
        else:
            resp.json.return_value = {}
        return resp

    mock_client.get.side_effect = _get_side_effect
    return mock_client


def _mock_client_for_failures(*, rows: list[dict], total_count: int) -> MagicMock:
    """Build a mock LumiverbClient for failures command."""
    mock_client = MagicMock()

    def _get_side_effect(path: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        if path == "/v1/libraries":
            resp.json.return_value = [{"library_id": "lib_01", "name": "Test", "root_path": "/photos"}]
        elif path == "/v1/jobs/failures":
            resp.json.return_value = {"rows": rows, "total_count": total_count}
        else:
            resp.json.return_value = {}
        return resp

    mock_client.get.side_effect = _get_side_effect
    return mock_client


@pytest.mark.fast
def test_pipeline_status_groups_correctly() -> None:
    """
    Pipeline status shows correct counts from the API response.
    The API returns pre-pivoted stages; the CLI just renders them.
    """
    mock_client = _mock_client_for_status(
        pipeline_response={
            "library": "Test",
            "library_id": "lib_01",
            "total_assets": 100,
            "workers": 0,
            "stages": [
                {"name": "proxy", "label": "Proxy", "done": 90, "inflight": 0, "pending": 0, "failed": 10, "blocked": 0},
                {"name": "exif", "label": "EXIF", "done": 100, "inflight": 0, "pending": 0, "failed": 0, "blocked": 0},
            ],
        }
    )

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["status", "--library", "Test"])

    assert result.exit_code == 0
    assert "90" in result.output  # Proxy Done
    assert "10" in result.output  # Proxy Failed
    assert "100" in result.output  # EXIF Done / Total assets


@pytest.mark.fast
def test_failures_accepts_vision_alias() -> None:
    """--job-type vision resolves to ai_vision; API receives ai_vision."""
    mock_client = _mock_client_for_failures(
        rows=[{"rel_path": "Photos/foo.jpg", "error_message": "Vision failed"}],
        total_count=1,
    )

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["failures", "--library", "Test", "--job-type", "vision"])

    assert result.exit_code == 0
    # Verify the API was called with the resolved job_type
    failures_call = [c for c in mock_client.get.call_args_list if c.args[0] == "/v1/jobs/failures"]
    assert len(failures_call) == 1
    params = failures_call[0].kwargs.get("params", {})
    assert params["job_type"] == "ai_vision"


@pytest.mark.fast
def test_failures_truncates_long_errors() -> None:
    """Pass an error message > 60 chars; assert output truncates with '...'."""
    long_error = "A" * 70

    mock_client = _mock_client_for_failures(
        rows=[{"rel_path": "Photos/foo.jpg", "error_message": long_error}],
        total_count=1,
    )

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["failures", "--library", "Test", "--job-type", "ai_vision"])

    assert result.exit_code == 0
    # Rich uses Unicode ellipsis (…) when truncating
    assert "…" in result.output or "..." in result.output
    assert long_error not in result.output


@pytest.mark.fast
def test_failures_shows_retry_hint() -> None:
    """Invoke failures command via typer runner; assert output contains 'lumiverb enqueue'."""
    mock_client = _mock_client_for_failures(
        rows=[{"rel_path": "Photos/foo.jpg", "error_message": "Caption failed"}],
        total_count=1,
    )

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["failures", "--library", "Test", "--job-type", "ai_vision"])

    assert result.exit_code == 0
    assert "lumiverb enqueue" in result.output


@pytest.mark.fast
def test_status_hint_shows_worst_failure_type() -> None:
    """Hint shows the job type with the most failures (ai_vision, not proxy)."""
    mock_client = _mock_client_for_status(
        pipeline_response={
            "library": "Test",
            "library_id": "lib_01",
            "total_assets": 100,
            "workers": 0,
            "stages": [
                {"name": "proxy", "label": "Proxy", "done": 0, "inflight": 0, "pending": 0, "failed": 2, "blocked": 0},
                {"name": "ai_vision", "label": "Vision (AI)", "done": 0, "inflight": 0, "pending": 0, "failed": 50, "blocked": 0},
            ],
        }
    )

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["status", "--library", "Test"])

    assert result.exit_code == 0
    assert "vision" in result.output
    assert "lumiverb failures" in result.output
