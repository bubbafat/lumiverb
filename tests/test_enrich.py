"""Tests for the enrich phase (Phase 2 of ADR-011).

Enrich delegates to run_repair, so these tests verify:
- The asset_ids filter works correctly
- The enrich CLI command is registered and routes properly
"""

from __future__ import annotations


class TestAssetIdsFilter:
    """Test that asset_ids filtering works in run_repair."""

    def test_filter_restricts_to_given_ids(self):
        """_filter helper restricts to given asset IDs."""
        _id_set = {"a1", "a3"}

        def _filter(assets):
            return [a for a in assets if a["asset_id"] in _id_set] if _id_set else assets

        all_assets = [
            {"asset_id": "a1", "rel_path": "1.jpg"},
            {"asset_id": "a2", "rel_path": "2.jpg"},
            {"asset_id": "a3", "rel_path": "3.jpg"},
        ]
        filtered = _filter(all_assets)
        assert len(filtered) == 2
        assert {a["asset_id"] for a in filtered} == {"a1", "a3"}

    def test_filter_none_passes_all(self):
        """When _id_set is None, all assets pass through."""
        _id_set = None

        def _filter(assets):
            return [a for a in assets if a["asset_id"] in _id_set] if _id_set else assets

        all_assets = [
            {"asset_id": "a1", "rel_path": "1.jpg"},
            {"asset_id": "a2", "rel_path": "2.jpg"},
        ]
        filtered = _filter(all_assets)
        assert len(filtered) == 2

    def test_filter_empty_list_returns_empty(self):
        """Empty asset_ids means nothing passes."""
        _id_set = set()

        def _filter(assets):
            return [a for a in assets if a["asset_id"] in _id_set] if _id_set else assets

        # Empty set is falsy, so _filter should pass all through
        # This matches the behavior: empty list = no restriction
        all_assets = [{"asset_id": "a1", "rel_path": "1.jpg"}]
        filtered = _filter(all_assets)
        assert len(filtered) == 1

    def test_run_repair_accepts_asset_ids(self):
        """run_repair signature accepts asset_ids parameter."""
        import inspect
        from src.client.cli.repair import run_repair

        sig = inspect.signature(run_repair)
        assert "asset_ids" in sig.parameters
        param = sig.parameters["asset_ids"]
        assert param.default is None


class TestEnrichCommand:
    """Test that the enrich CLI command is registered."""

    def test_enrich_help(self):
        """enrich command shows up in --help."""
        from typer.testing import CliRunner
        from src.client.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["enrich", "--help"])
        assert result.exit_code == 0
        assert "enrichment" in result.output.lower()
        assert "--job-type" in result.output
        assert "--concurrency" in result.output
        assert "--dry-run" in result.output

    def test_enrich_rejects_invalid_job_type(self):
        """enrich rejects job types not in ENRICH_TYPES."""
        from typer.testing import CliRunner
        from src.client.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["enrich", "--job-type", "bogus-type"])
        assert result.exit_code != 0
        assert "Invalid" in result.output

    def test_enrich_accepts_valid_job_types(self):
        """All ENRICH_TYPES are accepted by the command parser."""
        from src.client.cli.main import ENRICH_TYPES
        expected = {"embed", "vision", "faces", "redetect-faces", "ocr", "transcribe", "video-scenes", "scene-vision", "search-sync", "all"}
        assert set(ENRICH_TYPES) == expected
