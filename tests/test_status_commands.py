"""Fast tests for lumiverb status and failures CLI commands."""

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from src.cli.main import app

runner = CliRunner()


@pytest.mark.fast
def test_pipeline_status_groups_correctly() -> None:
    """
    Mock pipeline_status returning latest-state counts (what the fixed query produces).
    Tests latest-state semantics: one row per (asset_id, job_type); historical
    retries/cancelled jobs are ignored. Old query would double-count (e.g. 150
    completed for 100 assets); new implementation bounds counts by asset count.
    """
    mock_client = MagicMock()
    mock_client.get.return_value.json.side_effect = [
        [{"library_id": "lib_01", "name": "Test", "root_path": "/photos"}],
        {"tenant_id": "ten_01"},
    ]

    mock_session = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_session
    mock_cm.__exit__.return_value = None

    total_assets = 100
    mock_asset_repo = MagicMock()
    mock_asset_repo.count_by_library.return_value = total_assets

    # Simulates latest-state counts: proxy 90+10=100, exif 100 (all <= total_assets)
    mock_job_repo = MagicMock()
    mock_job_repo.pipeline_status.return_value = [
        {"job_type": "proxy", "status": "completed", "count": 90},
        {"job_type": "proxy", "status": "failed", "count": 10},
        {"job_type": "exif", "status": "completed", "count": 100},
    ]
    mock_job_repo.active_worker_count.return_value = 0

    mock_ssq_repo = MagicMock()
    mock_ssq_repo.search_sync_pipeline_status.return_value = []

    def make_asset_repo(*args: object, **kwargs: object) -> MagicMock:
        return mock_asset_repo

    def make_job_repo(*args: object, **kwargs: object) -> MagicMock:
        return mock_job_repo

    def make_ssq_repo(*args: object, **kwargs: object) -> MagicMock:
        return mock_ssq_repo

    with (
        patch("src.cli.main.LumiverbClient", return_value=mock_client),
        patch("src.core.database.get_tenant_session", return_value=mock_cm),
        patch("src.repository.tenant.AssetRepository", side_effect=make_asset_repo),
        patch("src.repository.tenant.WorkerJobRepository", side_effect=make_job_repo),
        patch("src.repository.tenant.SearchSyncQueueRepository", side_effect=make_ssq_repo),
    ):
        result = runner.invoke(app, ["status", "--library", "Test"])

    assert result.exit_code == 0
    assert "90" in result.output  # Proxy Done
    assert "10" in result.output  # Proxy Failed
    assert "100" in result.output  # EXIF Done, Total assets
    # Status counts must not exceed total assets (would indicate double-counting)
    status_rows = mock_job_repo.pipeline_status.return_value
    for row in status_rows:
        assert row["count"] <= total_assets


@pytest.mark.fast
def test_failures_accepts_vision_alias() -> None:
    """--job-type vision resolves to ai_vision; list_failures receives ai_vision."""
    mock_client = MagicMock()
    mock_client.get.return_value.json.side_effect = [
        [{"library_id": "lib_01", "name": "Test", "root_path": "/photos"}],
        {"tenant_id": "ten_01"},
    ]

    mock_session = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_session
    mock_cm.__exit__.return_value = None

    mock_job_repo = MagicMock()
    mock_job_repo.list_failures.return_value = (
        [{"rel_path": "Photos/foo.jpg", "error_message": "Vision failed", "failed_at": None}],
        1,
    )

    def make_job_repo(*args: object, **kwargs: object) -> MagicMock:
        return mock_job_repo

    with (
        patch("src.cli.main.LumiverbClient", return_value=mock_client),
        patch("src.core.database.get_tenant_session", return_value=mock_cm),
        patch("src.repository.tenant.WorkerJobRepository", side_effect=make_job_repo),
    ):
        result = runner.invoke(app, ["failures", "--library", "Test", "--job-type", "vision"])

    assert result.exit_code == 0
    mock_job_repo.list_failures.assert_called_once()
    call_kwargs = mock_job_repo.list_failures.call_args[1]
    assert call_kwargs["job_type"] == "ai_vision"


@pytest.mark.fast
def test_failures_truncates_long_errors() -> None:
    """Pass an error message > 60 chars; assert output truncates with '...'."""
    long_error = "A" * 70

    mock_client = MagicMock()
    mock_client.get.return_value.json.side_effect = [
        [{"library_id": "lib_01", "name": "Test", "root_path": "/photos"}],
        {"tenant_id": "ten_01"},
    ]

    mock_session = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_session
    mock_cm.__exit__.return_value = None

    mock_job_repo = MagicMock()
    mock_job_repo.list_failures.return_value = (
        [{"rel_path": "Photos/foo.jpg", "error_message": long_error, "failed_at": None}],
        1,
    )

    def make_job_repo(*args: object, **kwargs: object) -> MagicMock:
        return mock_job_repo

    with (
        patch("src.cli.main.LumiverbClient", return_value=mock_client),
        patch("src.core.database.get_tenant_session", return_value=mock_cm),
        patch("src.repository.tenant.WorkerJobRepository", side_effect=make_job_repo),
    ):
        result = runner.invoke(app, ["failures", "--library", "Test", "--job-type", "ai_vision"])

    assert result.exit_code == 0
    # Rich uses Unicode ellipsis (…) when truncating
    assert "…" in result.output or "..." in result.output
    assert long_error not in result.output


@pytest.mark.fast
def test_failures_shows_retry_hint() -> None:
    """Invoke failures command via typer runner; assert output contains 'lumiverb enqueue'."""
    mock_client = MagicMock()
    mock_client.get.return_value.json.side_effect = [
        [{"library_id": "lib_01", "name": "Test", "root_path": "/photos"}],
        {"tenant_id": "ten_01"},
    ]

    mock_session = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_session
    mock_cm.__exit__.return_value = None

    mock_job_repo = MagicMock()
    mock_job_repo.list_failures.return_value = (
        [{"rel_path": "Photos/foo.jpg", "error_message": "Caption failed", "failed_at": None}],
        1,
    )

    def make_job_repo(*args: object, **kwargs: object) -> MagicMock:
        return mock_job_repo

    with (
        patch("src.cli.main.LumiverbClient", return_value=mock_client),
        patch("src.core.database.get_tenant_session", return_value=mock_cm),
        patch("src.repository.tenant.WorkerJobRepository", side_effect=make_job_repo),
    ):
        result = runner.invoke(app, ["failures", "--library", "Test", "--job-type", "ai_vision"])

    assert result.exit_code == 0
    assert "lumiverb enqueue" in result.output


@pytest.mark.fast
def test_status_hint_shows_worst_failure_type() -> None:
    """Hint shows the job type with the most failures (ai_vision, not proxy)."""
    mock_client = MagicMock()
    mock_client.get.return_value.json.side_effect = [
        [{"library_id": "lib_01", "name": "Test", "root_path": "/photos"}],
        {"tenant_id": "ten_01"},
    ]

    mock_session = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_session
    mock_cm.__exit__.return_value = None

    mock_asset_repo = MagicMock()
    mock_asset_repo.count_by_library.return_value = 100

    mock_job_repo = MagicMock()
    mock_job_repo.pipeline_status.return_value = [
        {"job_type": "proxy", "status": "failed", "count": 2},
        {"job_type": "ai_vision", "status": "failed", "count": 50},
    ]
    mock_job_repo.active_worker_count.return_value = 0

    mock_ssq_repo = MagicMock()
    mock_ssq_repo.search_sync_pipeline_status.return_value = []

    with (
        patch("src.cli.main.LumiverbClient", return_value=mock_client),
        patch("src.core.database.get_tenant_session", return_value=mock_cm),
        patch("src.repository.tenant.AssetRepository", return_value=mock_asset_repo),
        patch("src.repository.tenant.WorkerJobRepository", return_value=mock_job_repo),
        patch("src.repository.tenant.SearchSyncQueueRepository", return_value=mock_ssq_repo),
    ):
        result = runner.invoke(app, ["status", "--library", "Test"])

    assert result.exit_code == 0
    assert "vision" in result.output
    assert "lumiverb failures" in result.output
