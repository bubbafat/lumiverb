"""Tests for Phase 3: ingest convergence (scan + enrich).

Verifies that:
- ingest command routes through scan + enrich (not the old monolithic path)
- repair is an alias for enrich
- The old _process_and_ingest_one path has no callers
- skip_types works in run_repair
- scan returns asset IDs
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from src.cli.scan import ScanStats


class TestScanReturnsAssetIds:
    """Verify ScanStats collects scanned asset IDs."""

    def test_scanned_asset_ids_default_empty(self):
        stats = ScanStats()
        assert stats.scanned_asset_ids == []

    def test_scanned_asset_ids_collects(self):
        stats = ScanStats()
        with stats.lock:
            stats.scanned_asset_ids.append("id-1")
            stats.scanned_asset_ids.append("id-2")
        assert stats.scanned_asset_ids == ["id-1", "id-2"]


class TestIngestRoutesThruScanEnrich:
    """Verify ingest no longer calls run_ingest."""

    def test_ingest_imports_scan_not_old_ingest(self):
        """The ingest command imports run_scan, not run_ingest."""
        from src.cli.main import ingest
        source = inspect.getsource(ingest)
        assert "run_scan" in source
        assert "run_ingest" not in source

    def test_ingest_imports_run_repair(self):
        """The ingest command imports run_repair for enrichment."""
        from src.cli.main import ingest
        source = inspect.getsource(ingest)
        assert "run_repair" in source

    def test_old_path_has_no_callers(self):
        """run_ingest and _process_and_ingest_one are not called from main.py."""
        from src.cli import main
        source = inspect.getsource(main)
        # run_ingest should not appear anywhere in main.py
        assert "run_ingest" not in source


class TestRepairIsEnrichAlias:
    """Verify repair routes through the same code as enrich."""

    def test_repair_calls_run_repair(self):
        from src.cli.main import repair
        source = inspect.getsource(repair)
        assert "run_repair" in source

    def test_repair_help_mentions_alias(self):
        from typer.testing import CliRunner
        from src.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["repair", "--help"])
        assert result.exit_code == 0
        assert "alias" in result.output.lower()


class TestSkipTypes:
    """Verify skip_types parameter in run_repair."""

    def test_run_repair_accepts_skip_types(self):
        from src.cli.repair import run_repair
        sig = inspect.signature(run_repair)
        assert "skip_types" in sig.parameters
        assert sig.parameters["skip_types"].default is None

    @patch("src.cli.repair._page_missing")
    @patch("src.cli.repair.get_repair_summary")
    def test_skip_types_excludes_from_plan(self, mock_summary, mock_page):
        """When skip_types includes 'vision', vision repair is skipped."""
        from rich.console import Console
        from src.cli.repair import run_repair

        mock_summary.return_value = {
            "total_assets": 10,
            "missing_embeddings": 5,
            "missing_vision": 5,
        }
        # Return empty for any _page_missing call so we skip execution
        mock_page.return_value = []

        client = MagicMock()
        library = {"library_id": "lib-1", "name": "Test", "root_path": "/tmp/test"}
        console = Console(quiet=True)

        # With skip_types={"vision"}, only embed should be in the plan
        # Since _page_missing returns empty, nothing actually executes,
        # but the plan should not include vision
        run_repair(
            client, library,
            job_type="all",
            console=console,
            skip_types={"vision"},
        )

        # _page_missing should only be called for embed (not vision)
        calls = mock_page.call_args_list
        called_with_vision = any(
            call.kwargs.get("missing_vision", False) for call in calls
        )
        called_with_embed = any(
            call.kwargs.get("missing_embeddings", False) for call in calls
        )
        assert not called_with_vision, "vision should have been skipped"
        assert called_with_embed, "embed should not have been skipped"


class TestIngestCommand:
    """Test the ingest CLI command registration."""

    def test_ingest_help(self):
        from typer.testing import CliRunner
        from src.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["ingest", "--help"])
        assert result.exit_code == 0
        assert "scan" in result.output.lower()
        assert "enrich" in result.output.lower()
        assert "--skip-vision" in result.output
        assert "--skip-embeddings" in result.output
