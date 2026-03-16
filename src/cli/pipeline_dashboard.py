"""Rich live dashboard for pipeline run: status table + worker status."""

from __future__ import annotations

import time
from typing import Any

from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


# Flash duration in seconds
FLASH_DURATION = 2.0
# Braille spinner frames — cycles at the Live refresh rate (4 Hz)
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class _StatusRenderable:
    """
    Thin wrapper that calls dashboard._render_status() on every Rich Live refresh,
    so the spinner animates at 4 Hz even between data updates.
    """

    def __init__(self, dashboard: PipelineDashboard) -> None:
        self._dashboard = dashboard

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self._dashboard._render_status()


class PipelineDashboard:
    """
    Context manager that runs a Rich Live display with:
    (1) Status table: Stage | Pending | Done | Failed, with flash on progress/failure
    (2) Workers table: Worker | Status (idle/active/completed/error)

    When status polling fails, keeps last known stages and shows a warning in the status section.
    """

    def __init__(self, library_name: str, total_assets: int, log_path: str | None = None) -> None:
        self.library_name = library_name
        self.total_assets = total_assets
        self._log_path = log_path
        self._console = Console()
        self._live: Live | None = None
        self._layout: Layout | None = None
        # stages: list of {"name", "label", "done", "pending", "failed"[, "library_name"]}
        self._stages: list[dict[str, Any]] = []
        self._status_warning = False
        # Previous counts per flash key for flash detection
        self._prev_counts: dict[str, dict[str, int]] = {}
        # flash_key -> expiry time for green (progress) / red (failure) flash
        self._flash_green: dict[str, float] = {}
        self._flash_red: dict[str, float] = {}
        # worker_cmd -> {"label": str, "status": str, "order": int}
        # status: "idle" | "active" | "completed" | "error"
        self._worker_states: dict[str, dict[str, Any]] = {}

    def __enter__(self) -> PipelineDashboard:
        self._layout = Layout()
        self._layout.split_row(
            Layout(name="status"),
            Layout(name="workers"),
        )
        self._layout.size = self._top_height()
        # Use _StatusRenderable so the spinner animates on every Live refresh cycle.
        self._layout["status"].update(_StatusRenderable(self))
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
            self._layout.size = self._top_height()
            self._layout["workers"].update(self._render_workers())
            self._live.refresh()

    def update(
        self,
        stages: list[dict[str, Any]],
        log_line: str | None = None,
    ) -> None:
        """
        Update dashboard state and refresh the display.

        - If log_line contains "Status poll failed", set warning indicator.
        - If stages is non-empty, update stored stages, clear warning, and apply flash logic.
        """
        now = time.time()

        if log_line is not None and "Status poll failed" in log_line:
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
            self._layout.size = self._top_height()
            # Status panel auto-renders via _StatusRenderable; no explicit update needed.
            self._layout["workers"].update(self._render_workers())
            self._live.refresh()

    def _top_height(self) -> int:
        has_library = any(s.get("library_name") for s in self._stages)
        if has_library:
            # Each library adds a header row + one row per stage + section separator
            libs: dict[str, int] = {}
            for s in self._stages:
                lib = s.get("library_name", "")
                libs[lib] = libs.get(lib, 0) + 1
            status_h = len(libs) * 2 + sum(libs.values()) + 2  # headers + stages + border
        else:
            status_h = len(self._stages) + 4
        workers_h = len(self._worker_states) + 2
        return max(status_h, workers_h) + 2

    def _render_status(self) -> RenderableType:
        """Build status section. Called on every Rich Live refresh so the spinner animates."""
        now = time.time()
        has_library = any(s.get("library_name") for s in self._stages)

        if has_library:
            return self._render_status_multi_lib(now)
        return self._render_status_single_lib(now)

    def _render_status_single_lib(self, now: float) -> RenderableType:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Stage", style="bold")
        table.add_column("Pending", justify="right")
        table.add_column("Done", justify="right")
        table.add_column("Failed", justify="right")

        for s in self._stages:
            name = s.get("name", "")
            flash_key = name
            label = s.get("label", name)
            pending, done, failed = s.get("pending", 0), s.get("done", 0), s.get("failed", 0)
            style = self._flash_style(flash_key, now)
            if style:
                table.add_row(
                    f"[{style}]{label}[/]",
                    f"[{style}]{pending:,}[/]",
                    f"[{style}]{done:,}[/]",
                    f"[{style}]{failed:,}[/]",
                )
            else:
                table.add_row(label, f"{pending:,}", f"{done:,}", f"{failed:,}")

        header = f"[bold]Library: {self.library_name}[/]  Total assets: {self.total_assets:,}"
        if self._status_warning:
            return Group(
                Panel(
                    "[yellow]⚠ Status poll failed — showing last known state[/]",
                    style="yellow",
                    border_style="yellow",
                ),
                table,
            )
        if not self._stages:
            return Group(Text.from_markup(header), table)
        return Panel(table, title=Text.from_markup(header), border_style="blue")

    def _render_status_multi_lib(self, now: float) -> RenderableType:
        # Group stages by library_name preserving order
        libs: dict[str, list[dict[str, Any]]] = {}
        for s in self._stages:
            lib = s.get("library_name", "")
            if lib not in libs:
                libs[lib] = []
            libs[lib].append(s)

        table = Table(show_header=True, header_style="bold", show_edge=True)
        table.add_column("Stage", style="bold")
        table.add_column("Pending", justify="right")
        table.add_column("Done", justify="right")
        table.add_column("Failed", justify="right")

        spinner_frame = _SPINNER[int(now * 8) % len(_SPINNER)]
        first = True
        for lib_name, stages in libs.items():
            if not first:
                table.add_section()
            first = False

            total_pending = sum(s.get("pending", 0) for s in stages)
            total_failed = sum(s.get("failed", 0) for s in stages)
            lib_active = any(
                self._flash_green.get(f"{lib_name}:{s.get('name', '')}", 0) > now
                for s in stages
            )

            if lib_active:
                lib_header = Text()
                lib_header.append(f"{spinner_frame} ", style="bold green")
                lib_header.append(lib_name, style="bold green")
            elif total_pending > 0:
                lib_header = Text()
                lib_header.append("○ ", style="dim")
                lib_header.append(lib_name)
            elif total_failed > 0:
                lib_header = Text()
                lib_header.append("⚠ ", style="bold red")
                lib_header.append(lib_name, style="bold red")
            else:
                lib_header = Text()
                lib_header.append("✓ ", style="dim green")
                lib_header.append(lib_name, style="dim")

            # Library header row — Stage column holds the library name, counts blank
            table.add_row(lib_header, "", "", "")

            for s in stages:
                name = s.get("name", "")
                flash_key = f"{lib_name}:{name}"
                label = s.get("label", name)
                pending, done, failed = s.get("pending", 0), s.get("done", 0), s.get("failed", 0)
                style = self._flash_style(flash_key, now)
                indent = "  "
                if style:
                    table.add_row(
                        f"[{style}]{indent}{label}[/]",
                        f"[{style}]{pending:,}[/]",
                        f"[{style}]{done:,}[/]",
                        f"[{style}]{failed:,}[/]",
                    )
                else:
                    table.add_row(f"{indent}{label}", f"{pending:,}", f"{done:,}", f"{failed:,}")

        header = f"[bold]All Libraries[/]  Total assets: {self.total_assets:,}"
        if self._status_warning:
            return Group(
                Panel(
                    "[yellow]⚠ Status poll failed — showing last known state[/]",
                    style="yellow",
                    border_style="yellow",
                ),
                table,
            )
        return Panel(table, title=Text.from_markup(header), border_style="blue")

    def _flash_style(self, flash_key: str, now: float) -> str:
        if self._flash_green.get(flash_key, 0) > now:
            return "green"
        if self._flash_red.get(flash_key, 0) > now:
            return "red"
        return ""

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

        if self._log_path:
            # Add a dim footer row with the log file path.
            table.add_row(
                Text("Log file", style="dim"),
                Text(self._log_path, style="dim"),
            )

        return Panel(table, title="[bold]Workers[/]", border_style="dim")
