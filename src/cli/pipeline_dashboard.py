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
# Internal ring buffer size — larger than any realistic terminal
LOG_BUFFER_MAX = 500


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
        self._log_lines: list[str] = []  # ring buffer, capped at LOG_BUFFER_MAX
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
            Layout(name="top", minimum_size=6),
            Layout(name="log", ratio=1, minimum_size=8),
        )
        self._layout["top"].split_row(
            Layout(name="status"),
            Layout(name="workers"),
        )
        self._layout["top"].size = self._top_height()
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

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        """Format a duration in seconds as e.g. '2m 14s' or '45s'."""
        s = int(seconds)
        m, s = divmod(s, 60)
        return f"{m}m {s:02d}s" if m else f"{s}s"

    def set_worker_status(self, worker_cmd: str, label: str, status: str) -> None:
        """
        Set the status of a worker. Creates the entry if it doesn't exist (preserving
        insertion order). Refreshes the display.

        status: "idle" | "active" | "completed" | "error"
        """
        now = time.time()
        if worker_cmd not in self._worker_states:
            self._worker_states[worker_cmd] = {
                "label": label,
                "status": status,
                "order": len(self._worker_states),
                "started_at": now if status == "active" else None,
                "ended_at": None,
            }
        else:
            state = self._worker_states[worker_cmd]
            prev_status = state["status"]
            state["label"] = label
            state["status"] = status
            if status == "active":
                state["started_at"] = now
                state["ended_at"] = None
            elif status in ("completed", "error") and prev_status == "active":
                state["ended_at"] = now

        if self._live is not None and self._layout is not None:
            self._layout["top"].size = self._top_height()
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
            if len(self._log_lines) > LOG_BUFFER_MAX:
                self._log_lines = self._log_lines[-LOG_BUFFER_MAX:]
            if "Status poll failed" in log_line:
                self._status_warning = True

        if stages:
            self._status_warning = False
            for s in stages:
                name = s.get("name", "")
                library_name = s.get("library_name", "")
                flash_key = f"{library_name}:{name}" if library_name else name
                done = s.get("done", 0)
                pending = s.get("pending", 0)
                failed = s.get("failed", 0)
                prev = self._prev_counts.get(flash_key, {})
                prev_done = prev.get("done", 0)
                prev_pending = prev.get("pending", 0)
                prev_failed = prev.get("failed", 0)
                if pending < prev_pending or done > prev_done:
                    self._flash_green[flash_key] = now + FLASH_DURATION
                if failed > prev_failed:
                    self._flash_red[flash_key] = now + FLASH_DURATION
                self._prev_counts[flash_key] = {"done": done, "pending": pending, "failed": failed}
            self._stages = stages

        # Expire old flashes
        self._flash_green = {k: v for k, v in self._flash_green.items() if v > now}
        self._flash_red = {k: v for k, v in self._flash_red.items() if v > now}

        if self._live is not None and self._layout is not None:
            self._layout["top"].size = self._top_height()
            self._layout["status"].update(self._render_status())
            self._layout["workers"].update(self._render_workers())
            self._layout["log"].update(self._render_log())
            self._live.refresh()

    def _top_height(self) -> int:
        # Status panel: border (2) + table header + separator (2) + one row per stage
        status_h = len(self._stages) + 4
        # Workers panel: border (2) + one row per worker
        workers_h = len(self._worker_states) + 2
        return max(status_h, workers_h) + 2  # +2 lines breathing room

    def _render_status(self) -> Panel | Group:
        """Build status section: table (and optional warning)."""
        now = time.time()
        has_library = any(s.get("library_name") for s in self._stages)
        table = Table(show_header=True, header_style="bold")
        if has_library:
            table.add_column("Library", style="bold")
        table.add_column("Stage", style="bold")
        table.add_column("Pending", justify="right")
        table.add_column("Done", justify="right")
        table.add_column("Failed", justify="right")

        for s in self._stages:
            name = s.get("name", "")
            library_name = s.get("library_name", "")
            flash_key = f"{library_name}:{name}" if library_name else name
            label = s.get("label", name)
            pending = s.get("pending", 0)
            done = s.get("done", 0)
            failed = s.get("failed", 0)
            if self._flash_green.get(flash_key, 0) > now:
                style = "green"
            elif self._flash_red.get(flash_key, 0) > now:
                style = "red"
            elif flash_key in self._flash_red:
                style = "dim red"
            else:
                style = ""
            p_str = f"{pending:,}"
            d_str = f"{done:,}"
            f_str = f"{failed:,}"
            if style:
                row = [f"[{style}]{label}[/]", f"[{style}]{p_str}[/]", f"[{style}]{d_str}[/]", f"[{style}]{f_str}[/]"]
                if has_library:
                    row = [f"[{style}]{library_name}[/]"] + row
                table.add_row(*row)
            else:
                row = [label, p_str, d_str, f_str]
                if has_library:
                    row = [library_name] + row
                table.add_row(*row)

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
        now = time.time()
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Worker", style="bold")
        table.add_column("Status")

        for state in sorted(self._worker_states.values(), key=lambda s: s["order"]):
            label = state["label"]
            status = state["status"]
            started_at: float | None = state.get("started_at")
            ended_at: float | None = state.get("ended_at")
            if status == "active":
                elapsed = self._fmt_duration(now - started_at) if started_at else "…"
                status_text = Text(f"● active ({elapsed})", style="green")
            elif status == "completed":
                duration = self._fmt_duration(ended_at - started_at) if started_at and ended_at else ""
                suffix = f" in {duration}" if duration else ""
                status_text = Text(f"✓ completed{suffix}", style="white")
            elif status == "error":
                duration = self._fmt_duration(ended_at - started_at) if started_at and ended_at else ""
                suffix = f" after {duration}" if duration else ""
                status_text = Text(f"✗ error{suffix}", style="red")
            else:
                status_text = Text("idle", style="dim")
            table.add_row(label, status_text)

        return Panel(table, title="[bold]Workers[/]", border_style="dim")

    def _render_log(self) -> Panel:
        """Fill the log panel exactly: show as many lines as the panel can hold."""
        # Total terminal height minus top panel and 2 lines for the log panel's own border.
        top_size = self._layout["top"].size if self._layout is not None else 8
        available = max(1, self._console.size.height - (top_size or 0) - 2)
        lines = self._log_lines[-available:] if self._log_lines else ["(no output yet)"]
        content = "\n".join(lines)
        return Panel(content, title="[bold]Log[/]", border_style="dim")
