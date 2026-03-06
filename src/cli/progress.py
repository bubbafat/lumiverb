"""CLI progress display: LiveProgress for indeterminate tasks with counters."""

from __future__ import annotations

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn


class LiveProgressBar:
    """Bar handle yielded by LiveProgress context manager."""

    def __init__(
        self,
        console: Console,
        use_progress: bool,
        label: str,
        counters: list[str],
        progress: Progress | None,
        task_id: int | None,
    ) -> None:
        self._console = console
        self._use_progress = use_progress
        self._label = label
        self._counters = counters
        self._completed = 0
        self._counter_values: dict[str, int] = {c: 0 for c in counters}
        self._progress = progress
        self._task_id = task_id

    def _description(self) -> str:
        parts = [f"{self._label}…"]
        counter_strs = [f"{self._counter_values[c]:,} {c}" for c in self._counters]
        if counter_strs:
            parts.append("  ".join(counter_strs))
        return "  ".join(parts)

    def update(self, completed: int = 0, **counter_updates: int) -> None:
        """Increment completed and counter values."""
        self._completed += completed
        for k, v in counter_updates.items():
            if k in self._counter_values:
                self._counter_values[k] += v
        if self._use_progress and self._progress is not None and self._task_id is not None:
            self._progress.update(
                self._task_id,
                completed=self._completed,
                description=self._description(),
            )

    def finish(self) -> None:
        """Snap bar to current completed count (makes it determinate)."""
        if self._use_progress and self._progress is not None and self._task_id is not None:
            self._progress.update(
                self._task_id,
                total=max(self._completed, 1),
                completed=self._completed,
                description=self._description(),
            )


class LiveProgress:
    """
    Context manager for indeterminate progress with counters.
    Disabled when not a terminal.
    """

    def __init__(
        self,
        console: Console,
        *,
        label: str,
        counters: list[str],
    ) -> None:
        self._console = console
        self._label = label
        self._counters = counters
        self._use_progress = console.is_terminal
        self._progress: Progress | None = None

    def __enter__(self) -> LiveProgressBar:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self._console,
            disable=not self._use_progress,
        )
        self._progress.__enter__()
        task_id = self._progress.add_task(f"{self._label}…", total=None)
        return LiveProgressBar(
            self._console,
            self._use_progress,
            self._label,
            self._counters,
            self._progress,
            task_id,
        )

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if self._progress is not None:
            self._progress.__exit__(exc_type, exc_val, exc_tb)
