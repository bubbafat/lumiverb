"""CLI maintenance commands: cleanup, search-sync, cleanup-dismissed, upgrade."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from src.client.cli.client import LumiverbClient

console = Console()
maintenance_app = typer.Typer(help="Maintenance tasks: cleanup, search sync, prune dismissed people, tenant upgrades.")


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


@maintenance_app.command("cleanup-dismissed")
def cleanup_dismissed() -> None:
    """Delete dismissed people that have zero face matches.

    Runs across all tenants. Useful after redetect-faces or bulk face
    re-processing where old face records were replaced.
    """
    client = LumiverbClient()
    resp = client.post("/v1/upkeep/cleanup-dismissed")
    result = resp.json()
    deleted = result.get("deleted", 0)
    if deleted:
        console.print(f"  Deleted {deleted} empty dismissed people.")
    else:
        console.print("  No empty dismissed people found.")


@maintenance_app.command("upgrade")
def upgrade(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show pending steps without executing.")] = False,
    step_id: Annotated[str | None, typer.Option("--step", help="Run only a specific upgrade step ID.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Run a step even if preceding steps are not complete.")] = False,
    max_steps: Annotated[int, typer.Option("--max-steps", help="Cap on pending steps to execute (0 = all).")] = 0,
) -> None:
    """Run tenant-level upgrades (schema/backfill steps) idempotently."""
    from src.client.cli.progress import UnifiedProgress, UnifiedProgressSpec

    client = LumiverbClient()

    if not dry_run:
        maint_resp = client.get("/v1/tenant/maintenance/status")
        maint = maint_resp.json()
        if not maint.get("active"):
            console.print("[red]Maintenance mode is not active.[/red]")
            console.print("Enable it first:")
            console.print("  lumiverb admin maintenance --start --message 'Upgrading'")
            raise typer.Exit(1)

    status_resp = client.get("/v1/tenant/upgrade/status")
    status = status_resp.json()

    steps_total = int(status.get("steps_total", 0))
    if steps_total == 0:
        console.print("[dim]No upgrade steps are registered in this build.[/dim]")
        return

    pending_step_ids: list[str] = status.get("remaining_pending_step_ids", []) or []
    has_work: bool = bool(status.get("has_work", False))
    steps_info: list[dict] = status.get("steps", []) or []

    if dry_run:
        if step_id is not None:
            match = next((s for s in steps_info if s.get("step_id") == step_id), None)
            if match is None:
                console.print(f"[red]Unknown upgrade step: {step_id}[/red]")
                raise typer.Exit(1)
            console.print(f"Tenant upgrade dry-run for step: {step_id}")
            console.print(f"  Step status: {match.get('status')}")
            try:
                target_index = next(i for i, s in enumerate(steps_info) if s.get("step_id") == step_id)
            except StopIteration:
                target_index = -1
            pending_or_failed_preceding: list[str] = []
            if target_index > 0:
                for s in steps_info[:target_index]:
                    if s.get("status") in ("pending", "failed"):
                        pending_or_failed_preceding.append(s.get("step_id", ""))
            if pending_or_failed_preceding:
                console.print(f"  Preceding pending/failed steps: {len(pending_or_failed_preceding)}")
                for sid in pending_or_failed_preceding:
                    console.print(f"  - {sid}")
            else:
                console.print("  Preceding steps: ready")
            return

        if not has_work:
            console.print("Tenant upgrade: [green]no pending steps[/green].")
            return
        console.print(f"Tenant upgrade (dry-run): {len(pending_step_ids)} of {steps_total} steps pending.")
        for sid in pending_step_ids:
            console.print(f"  - {sid}")
        return

    if step_id is not None:
        match = next((s for s in steps_info if s.get("step_id") == step_id), None)
        if match is None:
            console.print(f"[red]Unknown upgrade step: {step_id}[/red]")
            raise typer.Exit(1)

        pending_or_failed_preceding = []
        try:
            target_index = next(i for i, s in enumerate(steps_info) if s.get("step_id") == step_id)
        except StopIteration:
            target_index = -1
        if target_index > 0:
            for s in steps_info[:target_index]:
                if s.get("status") in ("pending", "failed"):
                    pending_or_failed_preceding.append(s.get("step_id", ""))

        if pending_or_failed_preceding and not force:
            console.print(f"[red]Refusing to run {step_id}: preceding steps are not complete.[/red]")
            for sid in pending_or_failed_preceding:
                console.print(f"  - {sid}")
            console.print("Run without --step, or re-run with --force to override.")
            raise typer.Exit(1)

        if pending_or_failed_preceding and force:
            confirmed = typer.confirm(
                f"Run --force and execute step '{step_id}' with {len(pending_or_failed_preceding)} preceding step(s) not complete?",
                default=False,
            )
            if not confirmed:
                console.print("Aborted.")
                return

    name_by_id = {s.get("step_id", ""): s.get("display_name", s.get("step_id", "")) for s in steps_info}
    pending_count = len(pending_step_ids)
    executed_steps = 0
    failed = 0

    spec = UnifiedProgressSpec(
        label="Upgrading tenant",
        unit="steps",
        counters=["done", "failed"],
        total=pending_count,
    )
    with UnifiedProgress(console, spec) as bar:
        bar.update(completed=0, done=0, failed=0)
        if step_id is not None:
            label = name_by_id.get(step_id, step_id)
            bar.update(completed=0, description=f"{label}…", done=0, failed=0)
            resp = client.post(
                "/v1/tenant/upgrade/execute",
                json={"max_steps": 1, "step_id": step_id, "force": force},
            )
            data = resp.json()
            ran_steps = data.get("ran_steps", []) or []
            executed_steps += len(ran_steps)
            failed = int(data.get("failed_steps", failed))
            has_work = bool(data.get("has_work_after", False))
            bar.update(completed=executed_steps, done=executed_steps, failed=failed)
        else:
            remaining = list(pending_step_ids)
            while has_work and (max_steps <= 0 or executed_steps < max_steps):
                current_id = remaining[0] if remaining else None
                label = name_by_id.get(current_id, current_id) if current_id else "Upgrading tenant"
                bar.update(completed=executed_steps, description=f"{label}…", done=executed_steps, failed=failed)
                resp = client.post(
                    "/v1/tenant/upgrade/execute",
                    json={"max_steps": 1},
                )
                data = resp.json()
                ran_steps = data.get("ran_steps", []) or []
                executed_steps += len(ran_steps)
                if ran_steps and remaining:
                    remaining.pop(0)
                failed = int(data.get("failed_steps", failed))
                has_work = bool(data.get("has_work_after", False))
                bar.update(completed=executed_steps, done=executed_steps, failed=failed)

    if not has_work:
        console.print("Tenant upgrade: [green]completed[/green].")
    else:
        if step_id is not None:
            console.print(f"Tenant upgrade: step '{step_id}' executed (or skipped).")
        else:
            console.print(f"Tenant upgrade: stopped after {executed_steps} step(s).")
