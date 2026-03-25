"""Fast tests for lumiverb worker search-sync CLI command."""

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.cli.main import app

runner = CliRunner()


def _mock_client_for_search_sync(*, pending_count: int = 1, batch_responses: list[dict] | None = None) -> MagicMock:
    """Build a mock LumiverbClient whose .get/.post return the right shapes."""
    if batch_responses is None:
        batch_responses = [
            {"processed": True, "synced": 1, "skipped": 0},
            {"processed": False, "synced": 0, "skipped": 0},
        ]

    mock_client = MagicMock()

    def _get_side_effect(path: str, **kwargs: object) -> MagicMock:
        resp = MagicMock()
        if path == "/v1/libraries":
            resp.json.return_value = [{"library_id": "lib_TestLib01", "name": "TestLib", "root_path": "/path"}]
        elif path == "/v1/search-sync/pending":
            resp.json.return_value = {"count": pending_count}
        else:
            resp.json.return_value = {}
        return resp

    post_call_count = 0

    def _post_side_effect(path: str, **kwargs: object) -> MagicMock:
        nonlocal post_call_count
        resp = MagicMock()
        if path == "/v1/search-sync/process-batch":
            idx = min(post_call_count, len(batch_responses) - 1)
            resp.json.return_value = batch_responses[idx]
            post_call_count += 1
        elif path == "/v1/search-sync/resync":
            resp.json.return_value = {"enqueued": 5}
            resp.raise_for_status.return_value = None
        else:
            resp.json.return_value = {}
        return resp

    mock_client.get.side_effect = _get_side_effect
    mock_client.post.side_effect = _post_side_effect
    return mock_client


@pytest.mark.fast
def test_worker_search_sync_calls_api() -> None:
    """search-sync uses the API (not direct DB) to process batches."""
    mock_client = _mock_client_for_search_sync()

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["worker", "search-sync", "--library", "TestLib", "--once"])

    assert result.exit_code == 0
    # Should have called the libraries endpoint
    mock_client.get.assert_any_call("/v1/libraries")
    # Should have checked pending count
    called_paths = [c.args[0] for c in mock_client.get.call_args_list]
    assert "/v1/search-sync/pending" in called_paths
    # Should have called process-batch
    post_paths = [c.args[0] for c in mock_client.post.call_args_list]
    assert "/v1/search-sync/process-batch" in post_paths
    # Output should show synced count
    assert "Synced" in result.output
    assert "1" in result.output


@pytest.mark.fast
def test_worker_search_sync_empty_queue() -> None:
    """When pending count is 0 and --once, prints message and exits."""
    mock_client = _mock_client_for_search_sync(pending_count=0)

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(app, ["worker", "search-sync", "--library", "TestLib", "--once"])

    assert result.exit_code == 0
    assert "No pending items" in result.output


@pytest.mark.fast
def test_worker_search_sync_force_resync() -> None:
    """--force-resync calls the resync endpoint before processing."""
    mock_client = _mock_client_for_search_sync()

    with patch("src.cli.main.LumiverbClient", return_value=mock_client):
        result = runner.invoke(
            app,
            ["worker", "search-sync", "--library", "TestLib", "--once", "--force-resync"],
        )

    assert result.exit_code == 0
    post_paths = [c.args[0] for c in mock_client.post.call_args_list]
    assert "/v1/search-sync/resync" in post_paths
    assert "Re-enqueued" in result.output
