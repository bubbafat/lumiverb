"""`lumiverb archive`: clips out of sight, kept forever (Robert's model, Oct 8).

A person archives clips by id or every clip under a folder; a clip whose
file went missing is archived too, and comes back by itself when the file
does. Mirrors the web's Archive view.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.client.cli.client import LumiverbClient

archive_app = typer.Typer(help="Archive clips (out of sight, kept forever), list them, and bring them back.")
console = Console()


def clips(n: int) -> str:
    return f"{n} {'clip' if n == 1 else 'clips'}"


def library_id_for(client: LumiverbClient, library: str) -> str:
    """A library's id from its name or id; exits 1 if there's none."""
    for lib in client.get("/v1/libraries").json():
        if library in (lib.get("library_id"), lib.get("name")):
            return lib["library_id"]
    console.print(f"[red]Library not found: {escape(library)}[/red]")
    raise typer.Exit(1)


def pick(client: LumiverbClient, asset_ids: list[str] | None, library: str | None, folder: str | None) -> dict:
    """The request body that picks clips: ids, or a library's folder ("" is the whole library)."""
    by_folder = library is not None or folder is not None
    if bool(asset_ids) == by_folder:
        console.print("[red]Give clip ids, or --library with --folder (--folder '' for the whole library).[/red]")
        raise typer.Exit(2)
    if not by_folder:
        return {"asset_ids": asset_ids}
    if library is None or folder is None:
        console.print("[red]A folder needs --library and --folder.[/red]")
        raise typer.Exit(2)
    return {"library_id": library_id_for(client, library), "path": folder}


def hidden_table(*more: str) -> Table:
    """Clip, Library, Path and `more`: ids never cut short (they're what the
    other commands take), paths wrap instead."""
    table = Table()
    table.add_column("Clip", no_wrap=True, min_width=30)
    table.add_column("Library", overflow="fold")
    table.add_column("Path", overflow="fold")
    for name in more:
        table.add_column(name, no_wrap=True)
    return table


def day(iso: str | None) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d") if iso else ""


_ASSET_IDS = Annotated[list[str] | None, typer.Argument(help="Clip ids.", show_default=False)]
_LIBRARY = Annotated[str | None, typer.Option("--library", "-l", help="Library name or id, with --folder.")]
_FOLDER = Annotated[str | None, typer.Option("--folder", "-f", help="Every clip under this folder ('' for all).")]


@archive_app.command("add")
def archive_add(
    asset_ids: _ASSET_IDS = None,
    library: _LIBRARY = None,
    folder: _FOLDER = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask before archiving a folder.")] = False,
) -> None:
    """Archive clips: out of sight, kept forever with everything they have. Scans
    leave them archived. By id, or every clip under a folder (a file added to the
    folder later shows up as usual); a folder asks first."""
    client = LumiverbClient()
    body = pick(client, asset_ids, library, folder)
    if "path" in body and not yes:
        where = f"under '{folder}' in {library}" if folder else f"in {library}"
        if not typer.confirm(f"Archive every clip {where}? They leave browse and search, kept with "
                             "everything they have.", default=False):
            console.print("Aborted.")
            raise typer.Exit(0)
    data = client.post("/v1/assets/archive", json=body).json()
    console.print(f"Archived {clips(len(data.get('archived', [])))}.")
    skipped = data.get("skipped", [])
    if skipped:
        console.print(f"Skipped {clips(len(skipped))} not in sight (already archived, in the trash, or unknown): "
                      + ", ".join(skipped))


@archive_app.command("restore")
def archive_restore(asset_ids: _ASSET_IDS = None, library: _LIBRARY = None, folder: _FOLDER = None) -> None:
    """Unarchive clips a person archived, by id or under a folder. A missing file's
    clip comes back when its file does, not with this."""
    client = LumiverbClient()
    data = client.post("/v1/assets/unarchive", json=pick(client, asset_ids, library, folder)).json()
    console.print(f"Unarchived {clips(len(data.get('unarchived', [])))}.")
    skipped = data.get("skipped", [])
    if skipped:
        console.print(f"Skipped {clips(len(skipped))} not archived by a person (a missing file comes back "
                      "with its file): " + ", ".join(skipped))


@archive_app.command("list")
def archive_list(
    library: Annotated[str | None, typer.Option("--library", "-l", help="Library name or id.")] = None,
    folder: Annotated[str | None, typer.Option("--folder", "-f", help="Only clips under this folder.")] = None,
    missing: Annotated[bool, typer.Option("--missing", help="Only clips whose file went missing.")] = False,
    by_hand: Annotated[bool, typer.Option("--by-hand", help="Only clips a person archived.")] = False,
    limit: Annotated[int, typer.Option("--limit", help="Most clips to show.")] = 100,
) -> None:
    """List archived clips, most recently archived first."""
    if missing and by_hand:
        console.print("[red]--missing and --by-hand are mutually exclusive.[/red]")
        raise typer.Exit(2)
    client = LumiverbClient()
    params: dict[str, str | int] = {"kind": "missing" if missing else "by_hand" if by_hand else "all",
                                    "limit": max(1, min(limit, 500))}
    if library:
        params["library_id"] = library_id_for(client, library)
    if folder:
        params["path"] = folder
    page = client.get("/v1/archive", params=params).json()
    items = page.get("items", [])
    if not items:
        console.print("Nothing archived here.")
        return
    table = hidden_table("Archived", "")
    for item in items:
        table.add_row(item["asset_id"], escape(item["library_name"]), escape(item["rel_path"]),
                      day(item["archived_at"]), "file missing" if item.get("file_missing") else "")
    console.print(table)
    total = page.get("total", len(items))
    if total > len(items):
        console.print(f"Showing {len(items)} of {clips(total)}.")


@archive_app.command("delete-missing")
def archive_delete_missing(
    library: Annotated[str | None, typer.Option("--library", "-l", help="Only this library's.")] = None,
    folder: Annotated[str | None, typer.Option("--folder", "-f", help="Only under this folder.")] = None,
    remove_from_projects: Annotated[bool, typer.Option(
        "--remove-from-projects", help="Clips in projects leave them.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask: go ahead with however many there are.")] = False,
) -> None:
    """Delete for good the clips whose files went missing (admins only), with
    everything made or written for them. The server says how many first. A
    file that comes back afterwards is a new clip."""
    from src.client.cli.commands.trash import _error, projects_say_yes

    client = LumiverbClient()
    body: dict = {}
    if library:
        body["library_id"] = library_id_for(client, library)
    if folder:
        body["path"] = folder
    where = (f" in {library}" if library else "") + (f" under '{folder}'" if folder else "")
    while True:
        r = client.raw("DELETE", "/v1/archive/missing", json={**body, "remove_from_projects": remove_from_projects})
        if r.status_code < 400:
            break
        err = _error(r)
        details = err.get("details") or {}
        if r.status_code == 409 and err.get("code") == "confirm_delete_missing":
            n = int(details.get("count", 0))
            if not yes and not typer.confirm(f"Delete {clips(n)} whose files are missing{where} for good? "
                                             "This can't be undone.", default=False):
                console.print("Aborted.")
                raise typer.Exit(0)
            body["count"] = n
            body["missing_before"] = details.get("listed_at")  # nothing gone missing since
            continue
        if r.status_code == 409 and err.get("code") == "in_projects" and not remove_from_projects:
            if not projects_say_yes(details, yes=yes, what="deleted for good, they leave"):
                raise typer.Exit(2 if yes else 0)
            remove_from_projects = True
            continue
        console.print(f"[red]Couldn't delete them: {escape(err.get('message') or r.text)}[/red]")
        raise typer.Exit(1)
    deleted = r.json().get("deleted", 0)
    console.print(f"Deleted {clips(deleted)} whose files were missing, for good." if deleted
                  else f"No clips with missing files{where}.")
