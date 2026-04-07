"""Tests for CLI rationalization: removed commands, skip_types, scan asset IDs."""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from src.client.cli.scan import ScanStats


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


class TestRemovedCommands:
    """Verify ingest, repair, similar-image are removed."""

    def test_no_ingest_command(self):
        from typer.testing import CliRunner
        from src.client.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["ingest", "--help"])
        assert result.exit_code != 0

    def test_no_repair_command(self):
        from typer.testing import CliRunner
        from src.client.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["repair", "--help"])
        assert result.exit_code != 0

    def test_no_similar_image_command(self):
        from typer.testing import CliRunner
        from src.client.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["similar-image", "--help"])
        assert result.exit_code != 0

    def test_old_path_has_no_callers(self):
        """run_ingest is not referenced from main.py."""
        from src.client.cli import main
        source = inspect.getsource(main)
        assert "run_ingest" not in source


class TestSkipTypes:
    """Verify skip_types parameter in run_repair."""

    def test_run_repair_accepts_skip_types(self):
        from src.client.cli.repair import run_repair
        sig = inspect.signature(run_repair)
        assert "skip_types" in sig.parameters
        assert sig.parameters["skip_types"].default is None

    @patch("src.client.cli.repair._page_missing")
    @patch("src.client.cli.repair.get_repair_summary")
    def test_skip_types_excludes_from_plan(self, mock_summary, mock_page):
        """When skip_types includes 'vision', vision repair is skipped."""
        from rich.console import Console
        from src.client.cli.repair import run_repair

        mock_summary.return_value = {
            "total_assets": 10,
            "missing_embeddings": 5,
            "missing_vision": 5,
        }
        mock_page.return_value = []

        client = MagicMock()
        library = {"library_id": "lib-1", "name": "Test", "root_path": "/tmp/test"}
        console = Console(quiet=True)

        run_repair(
            client, library,
            job_type="all",
            console=console,
            skip_types={"vision"},
        )

        calls = mock_page.call_args_list
        called_with_vision = any(
            call.kwargs.get("missing_vision", False) for call in calls
        )
        called_with_embed = any(
            call.kwargs.get("missing_embeddings", False) for call in calls
        )
        assert not called_with_vision, "vision should have been skipped"
        assert called_with_embed, "embed should not have been skipped"


class TestUserSubcommand:
    """Verify user commands moved to user subgroup."""

    def test_user_help(self):
        from typer.testing import CliRunner
        from src.client.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["user", "--help"])
        assert result.exit_code == 0
        assert "create" in result.output
        assert "list" in result.output
        assert "set-role" in result.output
        assert "remove" in result.output

    def test_no_top_level_create_user(self):
        from typer.testing import CliRunner
        from src.client.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["create-user", "--help"])
        assert result.exit_code != 0


class TestSimilarMerged:
    """Verify similar accepts --image flag."""

    def test_similar_help_shows_image(self):
        from typer.testing import CliRunner
        from src.client.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["similar", "--help"])
        assert result.exit_code == 0
        assert "--image" in result.output
        assert "--asset-id" in result.output
        assert "--path" in result.output
