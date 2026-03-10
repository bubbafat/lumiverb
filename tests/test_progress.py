"""Fast tests for UnifiedProgress and UnifiedProgressBar."""

from unittest.mock import MagicMock

import pytest


@pytest.mark.fast
def test_unified_progress_indeterminate_counters_update() -> None:
    """UnifiedProgressBar with total=None updates counters and unit display."""
    from src.cli.progress import UnifiedProgressBar, UnifiedProgressSpec

    mock_progress = MagicMock()
    spec = UnifiedProgressSpec(
        label="Processing jobs",
        unit="jobs",
        counters=["done", "failed"],
        total=None,
    )
    bar = UnifiedProgressBar(mock_progress, 0, use_progress=True, spec=spec)

    bar.update(completed=1, done=1, failed=0)
    assert bar._completed == 1
    assert bar._total is None
    assert bar._counter_values == {"done": 1, "failed": 0}
    assert bar._unit_display() == "1 jobs"
    assert bar._counters_display() == "1 done  0 failed"

    bar.update(completed=2, done=1, failed=1)
    assert bar._completed == 2
    assert bar._counter_values == {"done": 1, "failed": 1}


@pytest.mark.fast
def test_unified_progress_determinate_n_m_display() -> None:
    """UnifiedProgressBar with total set shows N / M format."""
    from src.cli.progress import UnifiedProgressBar, UnifiedProgressSpec

    spec = UnifiedProgressSpec(
        label="Syncing",
        unit="assets",
        counters=["synced", "skipped"],
        total=100,
    )
    bar = UnifiedProgressBar(
        MagicMock(),
        task_id=0,
        use_progress=False,
        spec=spec,
    )
    bar._completed = 50
    bar._total = 100
    bar._counter_values = {"synced": 45, "skipped": 5}

    assert bar._unit_display() == "50 / 100 assets"
    assert bar._counters_display() == "45 synced  5 skipped"


@pytest.mark.fast
def test_unified_progress_non_terminal_disabled() -> None:
    """UnifiedProgress with non-terminal does not render (progress disabled)."""
    from io import StringIO

    from rich.console import Console

    from src.cli.progress import UnifiedProgress, UnifiedProgressSpec

    # Console with file output is typically non-interactive (is_terminal=False)
    console = Console(file=StringIO(), force_terminal=False)
    spec = UnifiedProgressSpec(
        label="Test",
        unit="items",
        counters=[],
        total=None,
    )

    with UnifiedProgress(console, spec) as bar:
        bar.update(completed=10)
        bar.finish()
    # Should not raise; progress is disabled when not a terminal


@pytest.mark.fast
def test_unified_progress_finish_snaps_total() -> None:
    """finish() makes indeterminate bar determinate by setting total."""
    from src.cli.progress import UnifiedProgressBar, UnifiedProgressSpec

    mock_progress = MagicMock()
    spec = UnifiedProgressSpec(
        label="Test",
        unit="items",
        counters=[],
        total=None,
    )
    bar = UnifiedProgressBar(mock_progress, 0, use_progress=True, spec=spec)
    bar._completed = 42
    bar._total = None

    bar.finish()

    assert bar._total == 42
    mock_progress.update.assert_called_once()
    call_kw = mock_progress.update.call_args[1]
    assert call_kw["total"] == 42
    assert call_kw["completed"] == 42


@pytest.mark.fast
def test_unified_progress_empty_counters() -> None:
    """Counters can be empty; display is blank."""
    from src.cli.progress import UnifiedProgressBar, UnifiedProgressSpec

    spec = UnifiedProgressSpec(
        label="Enqueuing",
        unit="assets",
        counters=[],
        total=None,
    )
    bar = UnifiedProgressBar(MagicMock(), 0, use_progress=False, spec=spec)
    bar._completed = 100
    bar._total = 100

    assert bar._counters_display() == ""
