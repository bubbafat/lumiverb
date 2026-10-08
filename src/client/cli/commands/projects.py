"""CLI commands for managing projects."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from src.client.cli.client import LumiverbClient

console = Console()
projects_app = typer.Typer(help="Manage projects.")


@projects_app.command("list")
def project_list(
    json: Annotated[bool, typer.Option("--json", help="Output raw JSON.")] = False,
    archived: Annotated[bool, typer.Option("--archived", help="List archived projects instead.")] = False,
    all_: Annotated[bool, typer.Option("--all", help="List active and archived projects.")] = False,
) -> None:
    """List projects you own or that are shared with you (active ones unless asked)."""
    if archived and all_:
        console.print("[red]--archived and --all are mutually exclusive.[/red]")
        raise typer.Exit(1)
    client = LumiverbClient()
    if archived or all_:
        resp = client.get("/v1/projects", params={"status": "archived" if archived else "all"})
    else:
        resp = client.get("/v1/projects")
    data = resp.json()
    items = data.get("items", [])

    if json:
        import json as _json
        console.print(_json.dumps(items, indent=2))
        return

    table = Table(title="Projects")
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Assets", justify="right")
    table.add_column("Visibility")
    table.add_column("Ownership")
    table.add_column("Status")
    for col in items:
        table.add_row(
            col.get("project_id", ""),
            col.get("name", ""),
            str(col.get("asset_count", 0)),
            col.get("visibility", ""),
            col.get("ownership", ""),
            col.get("status", "active"),
        )
    console.print(table)


@projects_app.command("create")
def project_create(
    name: Annotated[str, typer.Option("--name", "-n", help="Project name.")],
    description: Annotated[str | None, typer.Option("--description", "-d", help="Optional description.")] = None,
    visibility: Annotated[str, typer.Option("--visibility", help="private, shared, or public.")] = "private",
) -> None:
    """Create a new project."""
    if visibility not in ("private", "shared", "public"):
        console.print("[red]Visibility must be 'private', 'shared', or 'public'.[/red]")
        raise typer.Exit(1)

    body: dict = {"name": name, "visibility": visibility}
    if description:
        body["description"] = description

    client = LumiverbClient()
    resp = client.post("/v1/projects", json=body)
    data = resp.json()
    console.print(f"[green]Project created: {data.get('project_id', '')}[/green]")
    console.print(f"  name: {data.get('name', name)}")
    if data.get("description"):
        console.print(f"  description: {data['description']}")
    console.print(f"  visibility: {data.get('visibility', visibility)}")


@projects_app.command("show")
def project_show(
    project_id: Annotated[str, typer.Option("--id", help="Project ID (prj_..., or col_... from before the rename).")],
    json: Annotated[bool, typer.Option("--json", help="Output raw JSON.")] = False,
) -> None:
    """Show project details and its assets."""
    client = LumiverbClient()
    resp = client.get(f"/v1/projects/{project_id}")
    col = resp.json()

    if json:
        # Also fetch assets
        assets_resp = client.get(f"/v1/projects/{project_id}/assets?limit=1000")
        assets_data = assets_resp.json()
        import json as _json
        console.print(_json.dumps({"project": col, "assets": assets_data}, indent=2))
        return

    console.print(f"[bold]{col.get('name', '')}[/bold]")
    if col.get("description"):
        console.print(f"  {col['description']}")
    console.print(f"  ID: {col.get('project_id', '')}")
    console.print(f"  Assets: {col.get('asset_count', 0)}")
    console.print(f"  Visibility: {col.get('visibility', '')}")
    console.print(f"  Sort: {col.get('sort_order', '')}")
    console.print()

    # List assets
    assets_resp = client.get(f"/v1/projects/{project_id}/assets?limit=50")
    assets_data = assets_resp.json()
    items = assets_data.get("items", [])
    if not items:
        console.print("  [dim]No assets in this project.[/dim]")
        return

    table = Table(title="Assets")
    table.add_column("Asset ID", style="dim")
    table.add_column("Path")
    table.add_column("Type")
    for asset in items:
        table.add_row(
            asset.get("asset_id", ""),
            asset.get("rel_path", ""),
            asset.get("media_type", ""),
        )
    console.print(table)
    if assets_data.get("next_cursor"):
        remaining = col.get("asset_count", 0) - len(items)
        if remaining > 0:
            console.print(f"  [dim]... and {remaining} more[/dim]")


@projects_app.command("add")
def project_add(
    project_id: Annotated[str, typer.Option("--id", help="Project ID (prj_..., or col_... from before the rename).")],
    asset_id: Annotated[list[str], typer.Option("--asset-id", help="Asset ID(s) to add. Repeat for multiple.")],
) -> None:
    """Add assets to a project."""
    client = LumiverbClient()
    resp = client.post(
        f"/v1/projects/{project_id}/assets",
        json={"asset_ids": asset_id},
    )
    data = resp.json()
    added = data.get("added", 0)
    console.print(f"[green]Added {added} asset(s) to project.[/green]")


@projects_app.command("remove")
def project_remove(
    project_id: Annotated[str, typer.Option("--id", help="Project ID (prj_..., or col_... from before the rename).")],
    asset_id: Annotated[list[str], typer.Option("--asset-id", help="Asset ID(s) to remove. Repeat for multiple.")],
) -> None:
    """Remove assets from a project."""
    client = LumiverbClient()
    resp = client.delete(
        f"/v1/projects/{project_id}/assets",
        json={"asset_ids": asset_id},
    )
    data = resp.json()
    removed = data.get("removed", 0)
    console.print(f"[green]Removed {removed} asset(s) from project.[/green]")


@projects_app.command("delete")
def project_delete(
    project_id: Annotated[str, typer.Option("--id", help="Project ID (prj_..., or col_... from before the rename).")],
) -> None:
    """Delete a project. Source assets are not affected."""
    confirm = typer.confirm(f"Delete project {project_id}?", default=False)
    if not confirm:
        console.print("Aborted.")
        raise typer.Exit(0)

    client = LumiverbClient()
    resp = client.raw("DELETE", f"/v1/projects/{project_id}")
    if resp.status_code == 204:
        console.print(f"[green]Deleted {project_id}[/green]")
        return
    client._handle_response(resp)  # type: ignore[attr-defined]


def _set_status(project_id: str, status: str) -> None:
    client = LumiverbClient()
    data = client.patch(f"/v1/projects/{project_id}", json={"status": status}).json()
    verb = "archived" if status == "archived" else "restored"
    console.print(f"[green]Project {verb}: {data.get('name', project_id)} ({project_id})[/green]")


@projects_app.command("archive")
def project_archive(
    project_id: Annotated[str, typer.Option("--id", help="Project ID.")],
) -> None:
    """Archive a project: it leaves the sidebar and pickers but keeps its clips."""
    _set_status(project_id, "archived")


@projects_app.command("restore")
def project_restore(
    project_id: Annotated[str, typer.Option("--id", help="Project ID.")],
) -> None:
    """Restore an archived project."""
    _set_status(project_id, "active")


_EXPORT_FORMATS = ("fcp7", "fcpxml")


@projects_app.command("export")
def project_export(
    project_id: Annotated[str, typer.Option("--id", help="Project ID.")],
    format: Annotated[str, typer.Option("--format", "-f", help="fcp7 (DaVinci Resolve / Premiere Pro) or fcpxml (Final Cut Pro).")],
    prefix: Annotated[str | None, typer.Option("--prefix", help="Where the originals live on the editing machine (default: each library's root).")] = None,
    output: Annotated[str | None, typer.Option("--output", "-o", help="File to write (default: the project name, in the current directory).")] = None,
) -> None:
    """Export a project as a bin of master clips for an editor."""
    import re
    from pathlib import Path

    if format not in _EXPORT_FORMATS:
        console.print(f"[red]--format must be one of: {', '.join(_EXPORT_FORMATS)}[/red]")
        raise typer.Exit(1)

    params = {"format": format}
    if prefix:
        params["prefix"] = prefix
    client = LumiverbClient()
    resp = client.get(f"/v1/projects/{project_id}/export", params=params)

    if output is None:
        from urllib.parse import unquote

        disposition = resp.headers.get("content-disposition", "")
        utf8 = re.search(r"filename\*=UTF-8''([^;]+)", disposition)
        plain = re.search(r'filename="([^"]+)"', disposition)
        if utf8:
            output = unquote(utf8.group(1))
        elif plain:
            output = plain.group(1)
        else:
            output = f"{project_id}.{'xml' if format == 'fcp7' else 'fcpxml'}"
    path = Path(output)
    path.write_bytes(resp.content)
    console.print(f"[green]Exported to {path}[/green]")

    skipped = int(resp.headers.get("x-lumiverb-skipped-stills", "0") or 0)
    if skipped:
        console.print(f"[yellow]{skipped} photos weren't included: exports are video only for now.[/yellow]")
    no_duration = int(resp.headers.get("x-lumiverb-skipped-no-duration", "0") or 0)
    if no_duration:
        console.print(f"[yellow]{no_duration} videos with no known length weren't included.[/yellow]")
    unprobed = int(resp.headers.get("x-lumiverb-unprobed", "0") or 0)
    if unprobed:
        console.print(
            f"[yellow]{unprobed} videos haven't been probed and use a default frame rate; "
            "run lumiverb enrich --job-type probe.[/yellow]"
        )
