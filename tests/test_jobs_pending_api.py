"""Fast tests for GET /v1/jobs/pending and WorkerJobRepository.pending_count."""

from unittest.mock import MagicMock

import pytest


@pytest.mark.fast
def test_pending_count_with_library_filter() -> None:
    """pending_count with library_id filters by library via JOIN active_assets."""
    from src.repository.tenant import WorkerJobRepository

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 7
    mock_session.execute.return_value = mock_result

    repo = WorkerJobRepository(mock_session)
    count = repo.pending_count(job_type="proxy", library_id="lib_abc")

    assert count == 7
    mock_session.execute.assert_called_once()
    args = mock_session.execute.call_args[0]
    stmt = str(args[0]).lower()
    assert "worker_jobs" in stmt
    assert "active_assets" in stmt
    assert "library_id" in stmt


@pytest.mark.fast
def test_pending_count_no_library_returns_all_job_type() -> None:
    """pending_count without library_id counts all pending/claimed for job_type."""
    from src.repository.tenant import WorkerJobRepository

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = 42
    mock_session.execute.return_value = mock_result

    repo = WorkerJobRepository(mock_session)
    count = repo.pending_count(job_type="ai_vision")

    assert count == 42
