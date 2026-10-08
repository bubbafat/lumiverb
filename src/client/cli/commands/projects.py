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
    trashed: Annotated[bool, typer.Option("--trashed", help="List your trash instead.")] = False,
) -> None:
    """List projects you own or that are shared with you (active ones unless asked)."""
    if sum((archived, all_, trashed)) > 1:
        console.print("[red]--archived, --all and --trashed are mutually exclusive.[/red]")
        raise typer.Exit(1)
    client = LumiverbClient()
    if archived or all_ or trashed:
        status = "archived" if archived else "trashed" if trashed else "all"
        resp = client.get("/v1/projects", params={"status": status})
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
    """Move a project to the trash. Its clips are not affected; restore it
    any time, or delete it for good with empty-trash."""
    client = LumiverbClient()
    resp = client.raw("DELETE", f"/v1/projects/{project_id}")
    if resp.status_code == 204:
        console.print(f"[green]Moved {project_id} to the trash.[/green]")
        console.print(f"[dim]Undo with: lumiverb project restore --id {project_id}[/dim]")
        return
    client._handle_response(resp)  # type: ignore[attr-defined]


def _set_status(project_id: str, status: str) -> None:
    client = LumiverbClient()
    data = client.patch(f"/v1/projects/{project_id}", json={"status": status}).json()
    verb = "archived" if status == "archived" else "restored"
    console.print(f"[green]Project {verb}: {data.get('name', project_id)} ({project_id})[/green]")


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _restore_clips(client: LumiverbClient, project_id: str) -> None:
    data = client.post(f"/v1/projects/{project_id}/restore-clips").json()
    restored, missing = data.get("restored", 0), data.get("missing", 0)
    console.print(f"[green]Restored {_plural(restored, 'clip', 'clips')} from the trash.[/green]")
    if missing:
        console.print(
            f"{_plural(missing, 'clip is', 'clips are')} missing from disk and will come back "
            "when the files do."
        )


@projects_app.command("archive")
def project_archive(
    project_id: Annotated[str, typer.Option("--id", help="Project ID.")],
) -> None:
    """Archive a project: it leaves the sidebar and pickers but keeps its clips."""
    _set_status(project_id, "archived")


@projects_app.command("restore")
def project_restore(
    project_id: Annotated[str, typer.Option("--id", help="Project ID.")],
    with_clips: Annotated[
        bool | None,
        typer.Option(
            "--with-clips/--without-clips",
            help="What to do with the project's clips in the trash; asked if they exist and neither is given.",
        ),
    ] = None,
) -> None:
    """Take a project out of the trash (back to active or archived, as it
    was), or restore an archived project to active."""
    client = LumiverbClient()
    path = f"/v1/projects/{project_id}/restore"
    resp = client.raw("POST", path, json={} if with_clips is None else {"with_clips": with_clips})
    if resp.status_code == 404:
        # Not in the trash: un-archive it, as before trash existed.
        _set_status(project_id, "active")
        return
    if resp.status_code == 409:
        # Clips in the trash: the server wants the user's choice.
        details = resp.json().get("error", {}).get("details", {})
        trashed = details.get("trashed_clips", 0)
        one = trashed == 1
        console.print(
            f"{_plural(trashed, 'clip', 'clips')} in this project {'is' if one else 'are'} in the trash. "
            f"Restoring {'it brings it' if one else 'them brings them'} back everywhere: "
            "the library, search and other projects."
        )
        with_clips = typer.confirm(f"Restore {'it' if one else 'them'} too?", default=False)
        resp = client.raw("POST", path, json={"with_clips": with_clips})
    client._handle_response(resp)  # type: ignore[attr-defined]
    data = resp.json()
    console.print(f"[green]Took {project_id} out of the trash.[/green]")
    restored, missing = data.get("restored_clips", 0), data.get("missing_clips", 0)
    left = data.get("trashed_clips", 0)
    if restored:
        console.print(f"[green]Restored {_plural(restored, 'clip', 'clips')} from the trash.[/green]")
    if left:
        console.print(
            f"Left {_plural(left, 'clip', 'clips')} in the trash; restore later with: "
            f"lumiverb project restore-clips --id {project_id}"
        )
    if missing:
        console.print(
            f"{_plural(missing, 'clip is', 'clips are')} missing from disk and will come back when the files do."
        )


@projects_app.command("restore-clips")
def project_restore_clips(
    project_id: Annotated[str, typer.Option("--id", help="Project ID.")],
) -> None:
    """Restore the project's clips that someone trashed. They come back
    everywhere: the library, search and any other project holding them."""
    _restore_clips(LumiverbClient(), project_id)


@projects_app.command("empty-trash")
def project_empty_trash(
    project_ids: Annotated[
        list[str] | None, typer.Option("--id", help="Only these trashed projects (repeatable).")
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Delete trashed projects for good: the named ones, or your whole trash.
    Their clips stay in your libraries."""
    client = LumiverbClient()
    trashed = client.get("/v1/projects", params={"status": "trashed"}).json().get("items", [])
    if project_ids:
        trashed = [p for p in trashed if p.get("project_id") in set(project_ids)]
    if not trashed:
        console.print("The trash is empty." if not project_ids else "None of those projects are in the trash.")
        return
    for p in trashed:
        console.print(f"  {p.get('name', '')} ({p.get('project_id', '')}, {p.get('asset_count', 0)} clips)")
    if not yes and not typer.confirm(
        f"Delete {_plural(len(trashed), 'project', 'projects')} for good? Their clips stay in your libraries.",
        default=False,
    ):
        console.print("Aborted.")
        return
    # Exactly the projects listed above, not whatever is in the trash by now.
    body = {"project_ids": [p["project_id"] for p in trashed]}
    deleted = client.post("/v1/projects/empty-trash", json=body).json().get("deleted", 0)
    console.print(f"[green]Deleted {_plural(deleted, 'project', 'projects')} for good.[/green]")


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
    trashed = int(resp.headers.get("x-lumiverb-skipped-trashed", "0") or 0)
    if trashed:
        console.print(
            f"[yellow]{_plural(trashed, 'clip in the trash wasn', 'clips in the trash weren')}'t included; "
            f"restore {'it' if trashed == 1 else 'them'} with: lumiverb project restore-clips --id {project_id}[/yellow]"
        )
    missing = int(resp.headers.get("x-lumiverb-skipped-missing", "0") or 0)
    if missing:
        console.print(
            f"[yellow]{_plural(missing, 'clip missing from disk wasn', 'clips missing from disk weren')}'t included.[/yellow]"
        )
    library_trashed = int(resp.headers.get("x-lumiverb-skipped-library-trashed", "0") or 0)
    if library_trashed:
        console.print(
            f"[yellow]{_plural(library_trashed, 'clip in a deleted library wasn', 'clips in a deleted library weren')}"
            "'t included.[/yellow]"
        )
    unprobed = int(resp.headers.get("x-lumiverb-unprobed", "0") or 0)
    if unprobed:
        console.print(
            f"[yellow]{unprobed} videos haven't been probed and use a default frame rate; "
            "run lumiverb enrich --job-type probe.[/yellow]"
        )
