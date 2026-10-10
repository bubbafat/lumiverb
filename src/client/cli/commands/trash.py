"""`lumiverb trash`: clips a person wants gone (Robert's model, Oct 8).

The trash keeps them, restorable, until it deletes them for good after the
account's trash days. Mirrors the web's Trash view. Libraries and projects
have their own trash (lumiverb library ..., lumiverb project ...).
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from src.client.cli.client import LumiverbClient
from src.client.cli.commands.archive import clips, day, hidden_table, library_id_for
from src.client.cli.decisions import in_projects, send_until_decided

trash_app = typer.Typer(help="Move clips to the trash, list it, restore from it, or delete for good.")
console = Console()


@trash_app.command("add")
def trash_add(
    asset_ids: Annotated[list[str], typer.Argument(help="Clip ids (in sight or archived).", show_default=False)],
    remove_from_projects: Annotated[bool, typer.Option(
        "--remove-from-projects", help="Clips in projects: hidden there now, and gone from them once deleted.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask; fail instead.")] = False,
) -> None:
    """Move clips to the trash. Archived clips can go too. They're deleted for good
    after the trash days (lumiverb settings show), restorable until then."""
    r = send_until_decided(
        LumiverbClient(), "DELETE", "/v1/assets",
        {"asset_ids": asset_ids, "reason": "user", "remove_from_projects": remove_from_projects},
        answers={"in_projects": in_projects(
            yes=yes, what="in the trash they're hidden there, and deleted for good they leave")},
        failed="Couldn't move them to the trash",
    )
    data = r.json()
    console.print(f"Moved {clips(len(data.get('trashed', [])))} to the trash.")
    others = data.get("not_found", [])
    if others:
        console.print(f"Skipped {clips(len(others))} (already in the trash, gone with a library, or unknown): "
                      + ", ".join(others))


@trash_app.command("restore")
def trash_restore(
    asset_ids: Annotated[list[str], typer.Argument(help="Clip ids.", show_default=False)],
) -> None:
    """Take clips out of the trash, back to where they were: in sight, or the
    archive for clips archived before. A library's clips come back with the library."""
    data = LumiverbClient().post("/v1/assets/restore", json={"asset_ids": asset_ids}).json()
    back = len(data.get("to_archive", []))
    console.print(f"Restored {clips(len(data.get('restored', [])))}."
                  + (f" {clips(back)} went back to the archive, where {'it was' if back == 1 else 'they were'}." if back else ""))
    skipped = data.get("skipped", [])
    if skipped:
        console.print(f"Skipped {clips(len(skipped))} not in the trash (or whose library is): " + ", ".join(skipped))


@trash_app.command("list")
def trash_list(
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name or id.")] = None,
    folder: Annotated[str | None, typer.Option("--folder", "-f", help="Only clips under this folder.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Most clips to show.")] = 100,
) -> None:
    """List clips in the trash, most recently trashed first, with when each is deleted for good."""
    client = LumiverbClient()
    params: dict[str, str | int] = {"limit": max(1, min(limit, 500))}
    if library:
        params["library_id"] = library_id_for(client, library)
    if folder:
        params["path"] = folder
    page = client.get("/v1/trash", params=params).json()
    items = page.get("items", [])
    if not items:
        console.print("The trash is empty.")
        return
    table = hidden_table("Trashed", "Deleted for good")
    for item in items:
        table.add_row(item["asset_id"], escape(item["library_name"]), escape(item["rel_path"]),
                      day(item["trashed_at"]), day(item.get("expires_at")) or "when emptied")
    console.print(table)
    total = page.get("total", len(items))
    if total > len(items):
        console.print(f"Showing {len(items)} of {clips(total)}.")
    if page.get("trash_days") is None:
        console.print("The trash is emptied by hand only (lumiverb trash empty).")


@trash_app.command("empty")
def trash_empty(
    asset_ids: Annotated[list[str] | None, typer.Argument(help="Clip ids; leave out with --all.", show_default=False)] = None,
    all_: Annotated[bool, typer.Option("--all", help="Every clip in the trash (of --library, --folder).")] = False,
    library: Annotated[str | None, typer.Option("--library", "-l", help="With --all: only this library's trash.")] = None,
    folder: Annotated[str | None, typer.Option("--folder", "-f", help="With --all: only under this folder.")] = None,
    remove_from_projects: Annotated[bool, typer.Option(
        "--remove-from-projects", help="Clips in projects leave them.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask; fail instead.")] = False,
) -> None:
    """Delete clips in the trash for good now, to free space (admins only). Never
    archived clips: move them to the trash first."""
    if bool(asset_ids) == all_ or ((library or folder) and not all_):
        console.print("[red]Give clip ids, or --all (with --library or --folder to narrow it).[/red]")
        raise typer.Exit(2)
    client = LumiverbClient()
    body: dict = {} if all_ else {"asset_ids": asset_ids}
    if library:
        body["library_id"] = library_id_for(client, library)
    if folder:
        body["path"] = folder
    where = (f" in {library}" if library else "") + (f" under '{folder}'" if folder else "")
    what = f"every clip in the trash{where}" if all_ else clips(len(asset_ids or []))
    if not yes and not typer.confirm(f"Delete {what} for good? This can't be undone.", default=False):
        console.print("Aborted.")
        raise typer.Exit(0)
    r = send_until_decided(
        client, "DELETE", "/v1/trash/empty", {**body, "remove_from_projects": remove_from_projects},
        answers={"in_projects": in_projects(yes=yes, what="deleted for good, they leave")},
        failed="Couldn't empty the trash",
    )
    console.print(f"Deleted {clips(r.json().get('deleted', 0))} for good.")
