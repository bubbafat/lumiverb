"""Tests for CLI rationalization: removed commands, skip_types, scan asset IDs."""

from __future__ import annotations

import inspect

from src.processing.scan import ScanStats


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
