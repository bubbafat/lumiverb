"""Unit tests for --apply-filters / --dry-run scan command flags."""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from typer.testing import CliRunner

from src.cli.main import app


def _scan_result(status: str = "complete", files_added: int = 0) -> MagicMock:
    r = MagicMock()
    r.status = status
    r.scan_id = "scan_1"
    r.files_discovered = 0
    r.files_added = files_added
    r.files_updated = 0
    r.files_skipped = 0
    r.files_missing = 0
    return r


def _make_client(
    *,
    filters: dict | None = None,
    assets_pages: list[list[dict]] | None = None,
) -> MagicMock:
    """Build a mock LumiverbClient for apply-filters tests."""
    client = MagicMock()

    filters_payload = filters or {"includes": [], "excludes": []}
    assets_pages = assets_pages or []

    get_responses: dict[str, MagicMock] = {}

    def _resp(status: int, body: object) -> MagicMock:
        m = MagicMock()
        m.status_code = status
        m.json.return_value = body
        return m

    # Library list
    client.get.return_value = _resp(200, [{"library_id": "lib_1", "name": "MyLib", "root_path": "/photos"}])

    # raw() is used for asset pagination in fetch_filter_candidates
    page_iter = iter(assets_pages)

    def _raw(method: str, path: str, **kwargs: object) -> MagicMock:
        if "/assets/page" in path:
            try:
                page = next(page_iter)
                return _resp(200, {"items": page, "next_cursor": None})
            except StopIteration:
                return _resp(200, {"items": [], "next_cursor": None})
        return _resp(200, {})

    client.raw.side_effect = _raw

    # _load_path_filters uses client.get for /filters, but client.get is also used
    # for library list. Override get to route by path.
    def _get(path: str, **kwargs: object) -> MagicMock:
        if "/filters" in path:
            return _resp(200, filters_payload)
        if "/scans/running" in path:
            return _resp(200, [])
        # library list (no path param differentiation needed — first call is library list)
        return _resp(200, [{"library_id": "lib_1", "name": "MyLib", "root_path": "/photos"}])

    client.get.side_effect = _get
    client.post.return_value = _resp(200, {"enqueued": 0})
    client.delete.return_value = _resp(200, {"trashed": 0})

    return client


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_dry_run_shows_candidates_no_changes() -> None:
    """--dry-run prints candidates and exits without trashing anything."""
    runner = CliRunner()
    client = _make_client(
        filters={"includes": [], "excludes": [{"pattern": "**/Proxy/**"}]},
        assets_pages=[
            [
                {"asset_id": "ast_01", "rel_path": "Photos/Proxy/p.jpg"},
                {"asset_id": "ast_02", "rel_path": "Photos/IMG_001.jpg"},
            ]
        ],
    )
    with patch("src.cli.main.LumiverbClient", return_value=client), patch(
        "src.cli.main.scan_library", return_value=_scan_result()
    ):
        result = runner.invoke(app, ["scan", "--library", "MyLib", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "ast_01" in result.output
    assert "Photos/Proxy/p.jpg" in result.output
    # The "good" asset must not appear as a candidate
    assert "ast_02" not in result.output
    assert "would be trashed" in result.output
    # No actual DELETE call
    client.delete.assert_not_called()


@pytest.mark.fast
def test_dry_run_no_candidates_message() -> None:
    """--dry-run with no failing assets prints zero-candidate summary."""
    runner = CliRunner()
    client = _make_client(
        filters={"includes": [{"pattern": "Photos/**"}], "excludes": []},
        assets_pages=[
            [{"asset_id": "ast_01", "rel_path": "Photos/IMG_001.jpg"}]
        ],
    )
    with patch("src.cli.main.LumiverbClient", return_value=client), patch(
        "src.cli.main.scan_library", return_value=_scan_result()
    ):
        result = runner.invoke(app, ["scan", "--library", "MyLib", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "0 asset(s) would be trashed" in result.output
    client.delete.assert_not_called()


# ---------------------------------------------------------------------------
# --apply-filters confirmed
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_apply_filters_confirmed_trashes_candidates() -> None:
    """--apply-filters with 'y' confirmation trashes only the failing assets."""
    runner = CliRunner()
    client = _make_client(
        filters={"includes": [], "excludes": [{"pattern": "**/Proxy/**"}]},
        assets_pages=[
            [
                {"asset_id": "ast_proxy", "rel_path": "Photos/Proxy/p.jpg"},
                {"asset_id": "ast_keep", "rel_path": "Photos/IMG_001.jpg"},
            ]
        ],
    )
    with patch("src.cli.main.LumiverbClient", return_value=client), patch(
        "src.cli.main.scan_library", return_value=_scan_result()
    ):
        result = runner.invoke(app, ["scan", "--library", "MyLib", "--apply-filters"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Trashed 1 asset(s)" in result.output
    client.delete.assert_called_once()
    delete_call = client.delete.call_args
    assert delete_call[0][0] == "/v1/assets"
    assert delete_call[1]["json"]["asset_ids"] == ["ast_proxy"]


@pytest.mark.fast
def test_apply_filters_batches_large_candidate_list() -> None:
    """DELETE is called in batches of APPLY_FILTERS_BATCH_SIZE (200)."""
    from src.cli.scanner import APPLY_FILTERS_BATCH_SIZE

    n = APPLY_FILTERS_BATCH_SIZE + 50  # 250 candidates
    assets = [
        {"asset_id": f"ast_{i:04d}", "rel_path": f"Proxy/img_{i:04d}.jpg"}
        for i in range(n)
    ]
    runner = CliRunner()
    client = _make_client(
        filters={"includes": [], "excludes": [{"pattern": "Proxy/**"}]},
        assets_pages=[assets],
    )
    with patch("src.cli.main.LumiverbClient", return_value=client), patch(
        "src.cli.main.scan_library", return_value=_scan_result()
    ):
        result = runner.invoke(app, ["scan", "--library", "MyLib", "--apply-filters"], input="y\n")

    assert result.exit_code == 0, result.output
    assert client.delete.call_count == 2
    first_batch = client.delete.call_args_list[0][1]["json"]["asset_ids"]
    second_batch = client.delete.call_args_list[1][1]["json"]["asset_ids"]
    assert len(first_batch) == APPLY_FILTERS_BATCH_SIZE
    assert len(second_batch) == 50
    assert f"Trashed {n:,} asset(s)" in result.output


# ---------------------------------------------------------------------------
# --apply-filters declined
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_apply_filters_declined_no_changes() -> None:
    """Declining the confirmation prompt leaves assets untouched."""
    runner = CliRunner()
    client = _make_client(
        filters={"includes": [], "excludes": [{"pattern": "**/Proxy/**"}]},
        assets_pages=[
            [{"asset_id": "ast_proxy", "rel_path": "Photos/Proxy/p.jpg"}]
        ],
    )
    with patch("src.cli.main.LumiverbClient", return_value=client), patch(
        "src.cli.main.scan_library", return_value=_scan_result()
    ):
        result = runner.invoke(app, ["scan", "--library", "MyLib", "--apply-filters"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Aborted" in result.output
    client.delete.assert_not_called()


# ---------------------------------------------------------------------------
# No filters configured
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_apply_filters_no_filters_configured_skips() -> None:
    """When the library has no filters, --apply-filters exits cleanly with a message."""
    runner = CliRunner()
    client = _make_client(
        filters={"includes": [], "excludes": []},
        assets_pages=[],
    )
    with patch("src.cli.main.LumiverbClient", return_value=client), patch(
        "src.cli.main.scan_library", return_value=_scan_result()
    ):
        result = runner.invoke(app, ["scan", "--library", "MyLib", "--apply-filters"])

    assert result.exit_code == 0, result.output
    assert "No filters configured" in result.output
    client.delete.assert_not_called()


@pytest.mark.fast
def test_dry_run_no_filters_configured_skips() -> None:
    """--dry-run with no filters also exits cleanly."""
    runner = CliRunner()
    client = _make_client(
        filters={"includes": [], "excludes": []},
        assets_pages=[],
    )
    with patch("src.cli.main.LumiverbClient", return_value=client), patch(
        "src.cli.main.scan_library", return_value=_scan_result()
    ):
        result = runner.invoke(app, ["scan", "--library", "MyLib", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "No filters configured" in result.output
    client.delete.assert_not_called()


# ---------------------------------------------------------------------------
# No candidates
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_apply_filters_nothing_to_trash() -> None:
    """When all assets pass the filters, --apply-filters exits cleanly."""
    runner = CliRunner()
    client = _make_client(
        filters={"includes": [{"pattern": "Photos/**"}], "excludes": []},
        assets_pages=[
            [
                {"asset_id": "ast_01", "rel_path": "Photos/a.jpg"},
                {"asset_id": "ast_02", "rel_path": "Photos/b.jpg"},
            ]
        ],
    )
    with patch("src.cli.main.LumiverbClient", return_value=client), patch(
        "src.cli.main.scan_library", return_value=_scan_result()
    ):
        result = runner.invoke(app, ["scan", "--library", "MyLib", "--apply-filters"])

    assert result.exit_code == 0, result.output
    assert "Nothing to trash" in result.output
    client.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Include filter: assets outside include set are candidates
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_apply_filters_include_filter_excludes_outside_assets() -> None:
    """Assets outside the include pattern are candidates for trashing."""
    runner = CliRunner()
    client = _make_client(
        filters={"includes": [{"pattern": "Photos/**"}], "excludes": []},
        assets_pages=[
            [
                {"asset_id": "ast_in", "rel_path": "Photos/a.jpg"},
                {"asset_id": "ast_out", "rel_path": "Videos/clip.mov"},
            ]
        ],
    )
    with patch("src.cli.main.LumiverbClient", return_value=client), patch(
        "src.cli.main.scan_library", return_value=_scan_result()
    ):
        result = runner.invoke(app, ["scan", "--library", "MyLib", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "ast_out" in result.output
    assert "Videos/clip.mov" in result.output
    assert "ast_in" not in result.output
    assert "1 asset(s) would be trashed" in result.output
