"""CLI maintenance commands: cleanup, search-sync."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from src.cli.client import LumiverbClient

console = Console()
maintenance_app = typer.Typer(help="Maintenance tasks: cleanup orphaned files, sync search index.")


@maintenance_app.command("cleanup")
def cleanup(
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name (optional, cleanup all if omitted).")] = None,
    execute: Annotated[bool, typer.Option("--execute", help="Actually delete files. Without this flag, only reports what would be deleted.")] = False,
) -> None:
    """Remove orphaned files left after trash is emptied.

    By default runs in dry-run mode and only reports what would be deleted.
    Pass --execute to actually delete files.
    """
    client = LumiverbClient()
    dry_run = not execute

    params = {"dry_run": str(dry_run).lower()}

    if library:
        # Resolve library name to confirm it exists
        libraries = client.get("/v1/libraries").json()
        match = next((lib for lib in libraries if lib["name"] == library), None)
        if match is None:
            console.print(f"[red]Library not found: {library}[/red]")
            raise typer.Exit(1)

    resp = client.post("/v1/upkeep/cleanup", params=params)
    result = resp.json()

    mode = "[bold red]EXECUTE[/bold red]" if execute else "[bold yellow]DRY RUN[/bold yellow]"
    console.print(f"\n  Cleanup mode: {mode}\n")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()

    table.add_row("Orphan tenant dirs", str(result.get("orphan_tenants", 0)))
    table.add_row("Orphan library dirs", str(result.get("orphan_libraries", 0)))
    table.add_row("Orphan files", str(result.get("orphan_files", 0)))

    bytes_freed = result.get("bytes_freed", 0)
    if bytes_freed > 1_000_000:
        size_str = f"{bytes_freed / 1_000_000:.1f} MB"
    elif bytes_freed > 1_000:
        size_str = f"{bytes_freed / 1_000:.1f} KB"
    else:
        size_str = f"{bytes_freed} bytes"
    table.add_row("Space freed" if execute else "Space to free", size_str)

    skipped = result.get("skipped_libraries", 0)
    if skipped:
        table.add_row("Skipped libraries", f"[yellow]{skipped}[/yellow]")

    console.print(table)

    errors = result.get("errors", [])
    if errors:
        console.print(f"\n  [yellow]Warnings ({len(errors)}):[/yellow]")
        for err in errors:
            console.print(f"    [dim]•[/dim] {err}")

    if not execute and (result.get("orphan_files", 0) or result.get("orphan_libraries", 0) or result.get("orphan_tenants", 0)):
        console.print("\n  [dim]Run with --execute to delete these files.[/dim]")

    console.print()


@maintenance_app.command("search-sync")
def search_sync(
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name (optional, sync all if omitted).")] = None,
) -> None:
    """Push stale assets to the Quickwit search index."""
    client = LumiverbClient()

    if library:
        libraries = client.get("/v1/libraries").json()
        match = next((lib for lib in libraries if lib["name"] == library), None)
        if match is None:
            console.print(f"[red]Library not found: {library}[/red]")
            raise typer.Exit(1)

    resp = client.post("/v1/upkeep/search-sync")
    result = resp.json()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()

    table.add_row("Assets synced", str(result.get("synced", 0)))
    table.add_row("Assets failed", str(result.get("failed", 0)))
    table.add_row("Scenes synced", str(result.get("scenes_synced", 0)))
    table.add_row("Scenes failed", str(result.get("scenes_failed", 0)))

    console.print()
    console.print(table)
    console.print()
