"""Fast tests for lumiverb worker search-sync CLI command."""

from unittest.mock import ANY, MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.cli.main import app

runner = CliRunner()


@pytest.mark.fast
def test_worker_search_sync_instantiates_and_runs() -> None:
    """Mock LumiverbClient and SearchSyncWorker; invoke search-sync; assert worker run_once called."""
    mock_client = MagicMock()
    # CLI order: first tenant context, then libraries (in _resolve_library_id).
    mock_client.get.return_value.json.side_effect = [
        {"tenant_id": "ten_01ABC"},
        [{"library_id": "lib_TestLib01", "name": "TestLib", "root_path": "/path"}],
    ]
    mock_session = MagicMock()
    mock_worker = MagicMock()
    mock_worker.run_once.return_value = {"synced": 1, "skipped": 0, "batches": 1}
    mock_worker.pending_count.return_value = 1

    mock_quickwit = MagicMock()
    mock_quickwit.enabled = True

    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_session
    mock_cm.__exit__.return_value = None

    with (
        patch("src.cli.main.LumiverbClient", return_value=mock_client),
        patch("src.core.database.get_tenant_session", return_value=mock_cm),
        patch("src.search.quickwit_client.QuickwitClient", return_value=mock_quickwit),
        patch("src.workers.search_sync.SearchSyncWorker", return_value=mock_worker) as mock_worker_cls,
    ):
        result = runner.invoke(app, ["worker", "search-sync", "--library", "TestLib", "--once"])

    assert result.exit_code == 0
    mock_client.get.assert_any_call("/v1/libraries")
    mock_client.get.assert_any_call("/v1/tenant/context")
    mock_worker_cls.assert_called_once_with(
        session=mock_session,
        library_id="lib_TestLib01",
        quickwit=ANY,
        path_prefix=None,
        output_mode="human",
    )
    mock_worker.pending_count.assert_called()
    mock_worker.run_once.assert_called_once()


@pytest.mark.fast
def test_worker_search_sync_output_jsonl_passes_output_mode() -> None:
    """With --output jsonl, SearchSyncWorker receives output_mode='jsonl'."""
    mock_client = MagicMock()
    mock_client.get.return_value.json.side_effect = [
        {"tenant_id": "ten_01ABC"},
        [{"library_id": "lib_TestLib01", "name": "TestLib", "root_path": "/path"}],
    ]
    mock_session = MagicMock()
    mock_worker = MagicMock()
    mock_worker.run_once.return_value = {"synced": 1, "skipped": 0, "batches": 1}
    mock_worker.pending_count.return_value = 1

    mock_quickwit = MagicMock()
    mock_quickwit.enabled = True
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_session
    mock_cm.__exit__.return_value = None

    with (
        patch("src.cli.main.LumiverbClient", return_value=mock_client),
        patch("src.core.database.get_tenant_session", return_value=mock_cm),
        patch("src.search.quickwit_client.QuickwitClient", return_value=mock_quickwit),
        patch("src.workers.search_sync.SearchSyncWorker", return_value=mock_worker) as mock_worker_cls,
    ):
        result = runner.invoke(
            app,
            ["worker", "search-sync", "--library", "TestLib", "--once", "--output", "jsonl"],
        )

    assert result.exit_code == 0
    mock_worker_cls.assert_called_once()
    call_kw = mock_worker_cls.call_args[1]
    assert call_kw.get("output_mode") == "jsonl"
