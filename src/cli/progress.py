"""CLI progress display: unified layout for all long-running commands.

UnifiedProgress provides spinner + bar + description + N/M units + counters,
matching the search-sync look. Supports determinate (total known) and
indeterminate (total=None) modes.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn


@dataclass
class UnifiedProgressSpec:
    """Specification for unified progress display."""

    label: str
    unit: str
    counters: list[str]
    total: int | None = None


class UnifiedProgressBar:
    """Handle for updating unified progress. Yielded by UnifiedProgress context manager."""

    def __init__(
        self,
        progress: Progress,
        task_id: int,
        use_progress: bool,
        spec: UnifiedProgressSpec,
    ) -> None:
        self._progress = progress
        self._task_id = task_id
        self._use_progress = use_progress
        self._spec = spec
        self._completed = 0
        self._total = spec.total
        self._counter_values: dict[str, int] = {c: 0 for c in spec.counters}

    def _unit_display(self) -> str:
        if self._total is not None:
            return f"{self._completed:,} / {self._total:,} {self._spec.unit}"
        return f"{self._completed:,} {self._spec.unit}"

    def _counters_display(self) -> str:
        parts = [f"{self._counter_values[c]:,} {c}" for c in self._spec.counters]
        return "  ".join(parts)

    def update(
        self,
        completed: int,
        total: int | None = None,
        **counter_values: int,
    ) -> None:
        """Update progress. Merges counter_values into internal counters."""
        self._completed = completed
        if total is not None:
            self._total = total
        for k, v in counter_values.items():
            if k in self._counter_values:
                self._counter_values[k] = v
        if self._use_progress:
            fields: dict[str, str] = {
                "unit_display": self._unit_display(),
                "counters": self._counters_display(),
            }
            self._progress.update(
                self._task_id,
                completed=self._completed,
                total=self._total,
                **fields,
            )

    def finish(self) -> None:
        """Snap total to completed (makes indeterminate bar determinate)."""
        if self._use_progress and self._total is None:
            self._total = max(self._completed, 1)
            fields: dict[str, str] = {
                "unit_display": self._unit_display(),
                "counters": self._counters_display(),
            }
            self._progress.update(
                self._task_id,
                completed=self._completed,
                total=self._total,
                **fields,
            )


class UnifiedProgress:
    """
    Context manager for unified progress display: spinner + bar + units + counters.
    Disabled when not a terminal.
    """

    def __init__(
        self,
        console: Console,
        spec: UnifiedProgressSpec,
    ) -> None:
        self._console = console
        self._spec = spec
        self._use_progress = console.is_terminal
        self._progress: Progress | None = None

    def __enter__(self) -> UnifiedProgressBar:
        self._progress = Progress(
            SpinnerColumn(),
            BarColumn(),
            TextColumn("[progress.description]{task.description}"),
            TextColumn("  "),
            TextColumn("{task.fields[unit_display]}"),
            TextColumn("  "),
            TextColumn("{task.fields[counters]}"),
            console=self._console,
            disable=not self._use_progress,
        )
        self._progress.__enter__()
        task_id = self._progress.add_task(
            f"{self._spec.label}…",
            total=self._spec.total,
            completed=0,
            unit_display="",
            counters="",
        )
        bar = UnifiedProgressBar(
            self._progress,
            task_id,
            self._use_progress,
            self._spec,
        )
        bar.update(0)
        return bar

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if self._progress is not None:
            self._progress.__exit__(exc_type, exc_val, exc_tb)
