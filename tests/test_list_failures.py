"""Fast tests for WorkerJobRepository.list_failures SQL semantics."""

from unittest.mock import MagicMock

import pytest


class _FakeResult:
    def __init__(self, rows=None, scalar_value=None) -> None:
        self._rows = rows or []
        self._scalar = scalar_value

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._scalar


@pytest.mark.fast
def test_list_failures_excludes_subsequently_completed() -> None:
    """
    Assets whose most recent job is completed must not be counted as failures.

    We simulate one asset whose job history is: failed -> failed -> completed.
    The SQL must only consider the latest job per asset and then filter to
    status='failed', which should yield total_count == 0 for this scenario.
    """
    from src.repository.tenant import WorkerJobRepository

    mock_session = MagicMock()

    def execute_side_effect(sql, params):
        sql_text = str(sql).lower()
        # Count query
        if "count(*)::int" in sql_text:
            # Ensure status filter moved to outer WHERE on latest.status
            assert "wj.status = 'failed'" not in sql_text
            assert "where latest.status = 'failed'" in sql_text
            # For a history ending in completed, latest.status != failed => count 0
            return _FakeResult(scalar_value=0)
        # Rows query
        assert "wj.status = 'failed'" not in sql_text
        assert "where latest.status = 'failed'" in sql_text
        # Latest is completed, so no failure rows should be returned.
        return _FakeResult(rows=[])

    mock_session.execute.side_effect = execute_side_effect

    repo = WorkerJobRepository(mock_session)
    rows, total = repo.list_failures(
        library_id="lib_test",
        job_type="ai_vision",
        path_prefix=None,
        limit=20,
    )

    assert total == 0
    assert rows == []


@pytest.mark.fast
def test_list_failures_includes_still_failed() -> None:
    """
    Assets whose most recent job is failed must be included.

    We simulate one asset whose job history is: failed -> completed -> failed.
    The latest status is failed, so total_count should be 1 and one row should
    be returned.
    """
    from datetime import datetime, timezone

    from src.repository.tenant import WorkerJobRepository

    mock_session = MagicMock()

    class Row:
        def __init__(self) -> None:
            self.rel_path = "photo.jpg"
            self.error_message = "boom"
            self.completed_at = datetime.now(timezone.utc)

    def execute_side_effect(sql, params):
        sql_text = str(sql).lower()
        # Count query
        if "count(*)::int" in sql_text:
            assert "wj.status = 'failed'" not in sql_text
            assert "where latest.status = 'failed'" in sql_text
            # Latest status is failed => count 1
            return _FakeResult(scalar_value=1)
        # Rows query
        assert "wj.status = 'failed'" not in sql_text
        assert "where latest.status = 'failed'" in sql_text
        return _FakeResult(rows=[Row()])

    mock_session.execute.side_effect = execute_side_effect

    repo = WorkerJobRepository(mock_session)
    rows, total = repo.list_failures(
        library_id="lib_test",
        job_type="ai_vision",
        path_prefix=None,
        limit=20,
    )

    assert total == 1
    assert len(rows) == 1
    assert rows[0]["rel_path"] == "photo.jpg"
    assert rows[0]["error_message"] == "boom"
    assert isinstance(rows[0]["failed_at"], datetime)

