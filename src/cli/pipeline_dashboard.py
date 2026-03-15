"""Rich live dashboard for pipeline run: status table + worker status + log panel."""

from __future__ import annotations

import time
from typing import Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# Flash duration in seconds
FLASH_DURATION = 2.0
LOG_LINES_MAX = 20


class PipelineDashboard:
    """
    Context manager that runs a Rich Live display with:
    (1) Status table: Stage | Pending | Done | Failed, with flash on progress/failure
    (2) Workers table: Worker | Status (idle/active/completed/error)
    (3) Log panel: last N lines of subprocess output.

    When status polling fails, keeps last known stages and shows a warning in the status section.
    """

    def __init__(self, library_name: str, total_assets: int) -> None:
        self.library_name = library_name
        self.total_assets = total_assets
        self._console = Console()
        self._live: Live | None = None
        self._layout: Layout | None = None
        # stages: list of {"name", "label", "done", "pending", "failed"}
        self._stages: list[dict[str, Any]] = []
        self._log_lines: list[str] = []
        self._status_warning = False
        # Previous counts per stage name for flash detection
        self._prev_counts: dict[str, dict[str, int]] = {}
        # stage_name -> expiry time for green (progress) / red (failure) flash
        self._flash_green: dict[str, float] = {}
        self._flash_red: dict[str, float] = {}
        # worker_cmd -> {"label": str, "status": str, "order": int}
        # status: "idle" | "active" | "completed" | "error"
        self._worker_states: dict[str, dict[str, Any]] = {}

    def __enter__(self) -> PipelineDashboard:
        self._layout = Layout()
        self._layout.split_column(
            Layout(name="status", minimum_size=6),
            Layout(name="workers", minimum_size=4),
            Layout(name="log", ratio=1, minimum_size=8),
        )
        self._live = Live(
            self._layout,
            console=self._console,
            refresh_per_second=4,
            screen=False,
        )
        self._live.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None
        self._layout = None

    def set_worker_status(self, worker_cmd: str, label: str, status: str) -> None:
        """
        Set the status of a worker. Creates the entry if it doesn't exist (preserving
        insertion order). Refreshes the display.

        status: "idle" | "active" | "completed" | "error"
        """
        if worker_cmd not in self._worker_states:
            self._worker_states[worker_cmd] = {
                "label": label,
                "status": status,
                "order": len(self._worker_states),
            }
        else:
            self._worker_states[worker_cmd]["label"] = label
            self._worker_states[worker_cmd]["status"] = status

        if self._live is not None and self._layout is not None:
            self._layout["workers"].update(self._render_workers())
            self._live.refresh()

    def update(
        self,
        stages: list[dict[str, Any]],
        log_line: str | None = None,
    ) -> None:
        """
        Update dashboard state and refresh the display.

        - If log_line is set, append to log buffer (trimmed to LOG_LINES_MAX).
        - If log_line contains "Status poll failed", set warning indicator.
        - If stages is non-empty, update stored stages, clear warning, and apply flash logic.
        """
        now = time.time()

        if log_line is not None:
            self._log_lines.append(log_line)
            if len(self._log_lines) > LOG_LINES_MAX:
                self._log_lines = self._log_lines[-LOG_LINES_MAX:]
            if "Status poll failed" in log_line:
                self._status_warning = True

        if stages:
            self._status_warning = False
            for s in stages:
                name = s.get("name", "")
                done = s.get("done", 0)
                pending = s.get("pending", 0)
                failed = s.get("failed", 0)
                prev = self._prev_counts.get(name, {})
                prev_done = prev.get("done", 0)
                prev_pending = prev.get("pending", 0)
                prev_failed = prev.get("failed", 0)
                if pending < prev_pending or done > prev_done:
                    self._flash_green[name] = now + FLASH_DURATION
                if failed > prev_failed:
                    self._flash_red[name] = now + FLASH_DURATION
                self._prev_counts[name] = {"done": done, "pending": pending, "failed": failed}
            self._stages = stages

        # Expire old flashes
        self._flash_green = {k: v for k, v in self._flash_green.items() if v > now}
        self._flash_red = {k: v for k, v in self._flash_red.items() if v > now}

        if self._live is not None and self._layout is not None:
            self._layout["status"].update(self._render_status())
            self._layout["workers"].update(self._render_workers())
            self._layout["log"].update(self._render_log())
            self._live.refresh()

    def _render_status(self) -> Panel | Group:
        """Build status section: table (and optional warning)."""
        now = time.time()
        table = Table(show_header=True, header_style="bold")
        table.add_column("Stage", style="bold")
        table.add_column("Pending", justify="right")
        table.add_column("Done", justify="right")
        table.add_column("Failed", justify="right")

        for s in self._stages:
            name = s.get("name", "")
            label = s.get("label", name)
            pending = s.get("pending", 0)
            done = s.get("done", 0)
            failed = s.get("failed", 0)
            if self._flash_green.get(name, 0) > now:
                style = "green"
            elif name in self._flash_red:
                if self._flash_red[name] > now:
                    style = "red"
                else:
                    style = "dim red"
            else:
                style = ""
            p_str = f"{pending:,}"
            d_str = f"{done:,}"
            f_str = f"{failed:,}"
            if style:
                table.add_row(
                    f"[{style}]{label}[/]",
                    f"[{style}]{p_str}[/]",
                    f"[{style}]{d_str}[/]",
                    f"[{style}]{f_str}[/]",
                )
            else:
                table.add_row(label, p_str, d_str, f_str)

        header = f"[bold]Library: {self.library_name}[/]  Total assets: {self.total_assets:,}"
        if self._status_warning:
            warning_panel = Panel(
                "[yellow]⚠ Status poll failed — showing last known state[/]",
                style="yellow",
                border_style="yellow",
            )
            return Group(warning_panel, table)
        if not self._stages:
            return Group(Text.from_markup(header), table)
        return Panel(
            table,
            title=Text.from_markup(header),
            border_style="blue",
        )

    def _render_workers(self) -> Panel:
        """Build workers section: compact table showing each worker's current status."""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Worker", style="bold")
        table.add_column("Status")

        for state in sorted(self._worker_states.values(), key=lambda s: s["order"]):
            label = state["label"]
            status = state["status"]
            if status == "active":
                status_text = Text("● active", style="green")
            elif status == "completed":
                status_text = Text("✓ completed", style="white")
            elif status == "error":
                status_text = Text("✗ error", style="red")
            else:
                status_text = Text("idle", style="dim")
            table.add_row(label, status_text)

        return Panel(table, title="[bold]Workers[/]", border_style="dim")

    def _render_log(self) -> Panel:
        """Build log panel from last LOG_LINES_MAX lines."""
        content = "\n".join(self._log_lines) if self._log_lines else "(no output yet)"
        return Panel(
            content,
            title="[bold]Log[/] (last {} lines)".format(LOG_LINES_MAX),
            border_style="dim",
        )
