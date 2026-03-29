"""CLI commands for managing collections."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from src.cli.client import LumiverbClient

console = Console()
collections_app = typer.Typer(help="Manage collections.")


@collections_app.command("list")
def collection_list(
    json: Annotated[bool, typer.Option("--json", help="Output raw JSON.")] = False,
) -> None:
    """List all collections you own or that are shared with you."""
    client = LumiverbClient()
    resp = client.get("/v1/collections")
    data = resp.json()
    items = data.get("items", [])

    if json:
        import json as _json
        console.print(_json.dumps(items, indent=2))
        return

    table = Table(title="Collections")
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Assets", justify="right")
    table.add_column("Visibility")
    table.add_column("Ownership")
    for col in items:
        table.add_row(
            col.get("collection_id", ""),
            col.get("name", ""),
            str(col.get("asset_count", 0)),
            col.get("visibility", ""),
            col.get("ownership", ""),
        )
    console.print(table)


@collections_app.command("create")
def collection_create(
    name: Annotated[str, typer.Option("--name", "-n", help="Collection name.")],
    description: Annotated[str | None, typer.Option("--description", "-d", help="Optional description.")] = None,
    visibility: Annotated[str, typer.Option("--visibility", help="private, shared, or public.")] = "private",
) -> None:
    """Create a new collection."""
    if visibility not in ("private", "shared", "public"):
        console.print("[red]Visibility must be 'private', 'shared', or 'public'.[/red]")
        raise typer.Exit(1)

    body: dict = {"name": name, "visibility": visibility}
    if description:
        body["description"] = description

    client = LumiverbClient()
    resp = client.post("/v1/collections", json=body)
    data = resp.json()
    console.print(f"[green]Collection created: {data.get('collection_id', '')}[/green]")
    console.print(f"  name: {data.get('name', name)}")
    if data.get("description"):
        console.print(f"  description: {data['description']}")
    console.print(f"  visibility: {data.get('visibility', visibility)}")


@collections_app.command("show")
def collection_show(
    collection_id: Annotated[str, typer.Option("--id", help="Collection ID (col_...).")],
    json: Annotated[bool, typer.Option("--json", help="Output raw JSON.")] = False,
) -> None:
    """Show collection details and its assets."""
    client = LumiverbClient()
    resp = client.get(f"/v1/collections/{collection_id}")
    col = resp.json()

    if json:
        # Also fetch assets
        assets_resp = client.get(f"/v1/collections/{collection_id}/assets?limit=1000")
        assets_data = assets_resp.json()
        import json as _json
        console.print(_json.dumps({"collection": col, "assets": assets_data}, indent=2))
        return

    console.print(f"[bold]{col.get('name', '')}[/bold]")
    if col.get("description"):
        console.print(f"  {col['description']}")
    console.print(f"  ID: {col.get('collection_id', '')}")
    console.print(f"  Assets: {col.get('asset_count', 0)}")
    console.print(f"  Visibility: {col.get('visibility', '')}")
    console.print(f"  Sort: {col.get('sort_order', '')}")
    console.print()

    # List assets
    assets_resp = client.get(f"/v1/collections/{collection_id}/assets?limit=50")
    assets_data = assets_resp.json()
    items = assets_data.get("items", [])
    if not items:
        console.print("  [dim]No assets in this collection.[/dim]")
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


@collections_app.command("add")
def collection_add(
    collection_id: Annotated[str, typer.Option("--id", help="Collection ID (col_...).")],
    asset_id: Annotated[list[str], typer.Option("--asset-id", help="Asset ID(s) to add. Repeat for multiple.")],
) -> None:
    """Add assets to a collection."""
    client = LumiverbClient()
    resp = client.post(
        f"/v1/collections/{collection_id}/assets",
        json={"asset_ids": asset_id},
    )
    data = resp.json()
    added = data.get("added", 0)
    console.print(f"[green]Added {added} asset(s) to collection.[/green]")


@collections_app.command("remove")
def collection_remove(
    collection_id: Annotated[str, typer.Option("--id", help="Collection ID (col_...).")],
    asset_id: Annotated[list[str], typer.Option("--asset-id", help="Asset ID(s) to remove. Repeat for multiple.")],
) -> None:
    """Remove assets from a collection."""
    client = LumiverbClient()
    resp = client.delete(
        f"/v1/collections/{collection_id}/assets",
        json={"asset_ids": asset_id},
    )
    data = resp.json()
    removed = data.get("removed", 0)
    console.print(f"[green]Removed {removed} asset(s) from collection.[/green]")


@collections_app.command("delete")
def collection_delete(
    collection_id: Annotated[str, typer.Option("--id", help="Collection ID (col_...).")],
) -> None:
    """Delete a collection. Source assets are not affected."""
    confirm = typer.confirm(f"Delete collection {collection_id}?", default=False)
    if not confirm:
        console.print("Aborted.")
        raise typer.Exit(0)

    client = LumiverbClient()
    resp = client.raw("DELETE", f"/v1/collections/{collection_id}")
    if resp.status_code == 204:
        console.print(f"[green]Deleted {collection_id}[/green]")
        return
    client._handle_response(resp)  # type: ignore[attr-defined]
