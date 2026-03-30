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
    force: Annotated[bool, typer.Option("--force", help="Clear timestamps and re-index everything.")] = False,
) -> None:
    """Push stale assets to the Quickwit search index.

    Use --force to clear all search_synced_at timestamps and re-index
    everything into the tenant index. Useful after index migrations.
    """
    client = LumiverbClient()

    if library:
        libraries = client.get("/v1/libraries").json()
        match = next((lib for lib in libraries if lib["name"] == library), None)
        if match is None:
            console.print(f"[red]Library not found: {library}[/red]")
            raise typer.Exit(1)

    total_synced = 0
    total_failed = 0
    total_scenes_synced = 0
    total_scenes_failed = 0
    batch = 0

    while True:
        batch += 1
        # Only pass force on the first batch (clears timestamps once)
        qs = "?force=true" if force and batch == 1 else ""
        resp = client.post(f"/v1/upkeep/search-sync{qs}")
        result = resp.json()

        synced = result.get("synced", 0)
        failed = result.get("failed", 0)
        scenes_synced = result.get("scenes_synced", 0)
        scenes_failed = result.get("scenes_failed", 0)

        total_synced += synced
        total_failed += failed
        total_scenes_synced += scenes_synced
        total_scenes_failed += scenes_failed

        if synced > 0 or scenes_synced > 0:
            console.print(f"  Batch {batch}: {synced} assets, {scenes_synced} scenes synced")

        # Done when nothing was processed
        if synced == 0 and failed == 0 and scenes_synced == 0 and scenes_failed == 0:
            break

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()

    table.add_row("Assets synced", str(total_synced))
    table.add_row("Assets failed", str(total_failed))
    table.add_row("Scenes synced", str(total_scenes_synced))
    table.add_row("Scenes failed", str(total_scenes_failed))

    console.print()
    console.print(table)
    console.print()
