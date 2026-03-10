"""Fast tests for search-sync progress, pending_count, and force-resync batching."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.fast
def test_pending_count_with_library_filter() -> None:
    """pending_count with library_id uses search_sync_latest view and JOIN assets."""
    from src.repository.tenant import SearchSyncQueueRepository

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 42
    mock_session.execute.return_value = mock_result

    repo = SearchSyncQueueRepository(mock_session)
    count = repo.pending_count(library_id="lib_abc123")

    assert count == 42
    mock_session.execute.assert_called_once()
    args, kwargs = mock_session.execute.call_args
    assert args[1] == {"library_id": "lib_abc123"}
    stmt = str(args[0]) if hasattr(args[0], "text") else str(args[0])
    assert "search_sync_latest" in stmt or "JOIN assets" in stmt


@pytest.mark.fast
def test_claim_batch_path_filter_applied() -> None:
    """claim_batch with path_prefix includes path condition in SQL."""
    from src.repository.tenant import SearchSyncQueueRepository

    mock_session = MagicMock()
    mock_session.execute.return_value.fetchall.return_value = []
    mock_session.get.return_value = None

    repo = SearchSyncQueueRepository(mock_session)
    repo.claim_batch(10, library_id="lib_x", path_prefix="Photos/2024")

    mock_session.execute.assert_called_once()
    args, _ = mock_session.execute.call_args
    stmt_text = str(args[0])
    params = args[1]
    assert "path_prefix" in params
    assert params["path_prefix"] == "Photos/2024/%"
    assert "rel_path LIKE" in stmt_text or "path_prefix" in stmt_text


@pytest.mark.fast
def test_force_resync_batches() -> None:
    """enqueue_all_for_library with 1100 assets calls progress_callback 3 times (500+500+100)."""
    from src.repository.tenant import SearchSyncQueueRepository

    asset_ids = [f"ast_{i:05d}" for i in range(1100)]

    mock_session = MagicMock()
    # 1st fetchall: SELECT assets returns 1100 rows; 2nd-4th: SELECT existing returns []
    mock_fetchall = MagicMock(
        side_effect=[
            [(aid,) for aid in asset_ids],
            [],  # batch 0 existing
            [],  # batch 1 existing
            [],  # batch 2 existing
        ]
    )
    mock_exec_result = MagicMock()
    mock_exec_result.fetchall = mock_fetchall
    mock_exec_result.scalar.return_value = 0
    mock_exec_result.rowcount = 0
    mock_session.execute.return_value = mock_exec_result

    repo = SearchSyncQueueRepository(mock_session)
    progress_calls: list[tuple[int, int]] = []

    def _progress(completed: int, total: int) -> None:
        progress_calls.append((completed, total))

    with patch.object(repo, "enqueue", return_value=MagicMock()):
        result = repo.enqueue_all_for_library(
            "lib_test", progress_callback=_progress
        )

    assert len(result) == 1100
    assert progress_calls == [(500, 1100), (1000, 1100), (1100, 1100)]
