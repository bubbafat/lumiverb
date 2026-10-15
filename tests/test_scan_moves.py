"""Moved files and the scan's flags for them.

The server finds moves now (ADR-018 decision 12): the same content at a new
path, gone from its old one, only files of a gone clip's size hashed for it,
never a copy (tests/test_scans_api.py; end to end, tests/test_scan_outcomes.py).
What the scan does about them (applies them, asks, skips them) is in
tests/test_scan.py. These are the CLI's flags.
"""

from __future__ import annotations

import pytest

from src.processing.scan import ScanStats


@pytest.mark.fast
class TestCliFlags:
    """Test --allow-moves and --skip-moves CLI validation."""

    def test_allow_and_skip_mutual_exclusion(self):
        """Both flags together should be rejected."""
        from typer.testing import CliRunner

        from src.client.cli.main import app
        runner = CliRunner()
        result = runner.invoke(app, [
            "scan", "--library", "test",
            "--allow-moves", "--skip-moves",
        ])
        assert result.exit_code != 0
        assert "Cannot use both" in result.output

    def test_run_scan_accepts_move_flags(self):
        """run_scan signature accepts allow_moves and skip_moves."""
        import inspect

        from src.processing.scan import run_scan
        sig = inspect.signature(run_scan)
        assert "allow_moves" in sig.parameters
        assert "skip_moves" in sig.parameters

    def test_skip_moves_says_what_it_does(self):
        """--skip-moves looks for no moves: a moved file is a new clip, and
        nothing is archived (pinned by tests/test_scan_outcomes.py)."""
        import typing

        from src.client.cli.main import scan
        option = typing.get_type_hints(scan, include_extras=True)["skip_moves"].__metadata__[0]
        assert "new clip" in option.help and "nothing is archived" in option.help


@pytest.mark.fast
class TestScanStatsIncludesMoved:
    """Verify ScanStats has the moved field."""

    def test_moved_field(self):
        stats = ScanStats()
        assert stats.moved == 0
