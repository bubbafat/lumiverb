"""`lumiverb producers`: what each producer has made, and upgrading what's stale.

Mirrors the web's Settings → Processing (ADR-016 phase 3, piece 5). An
artifact is current, missing, stale (made with an older producer or
settings) or failing. Stale ones are made again only once an admin
approves an upgrade; the API asks about clips with a person's edits, and
for a producer that must stay one model across the library (CLIP, faces)
for a confirmation naming the count.
"""

from __future__ import annotations

from typing import Annotated

import click
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.client.cli.client import LumiverbClient
from src.client.cli.commands.archive import clips, library_id_for

producers_app = typer.Typer(
    help="What each producer has made (current, missing, stale, failing), and upgrading what's stale.",
    invoke_without_command=True,
)
console = Console()

EDIT_CHOICES = ("keep", "replace", "skip")


def _project_id_for(client: LumiverbClient, project: str) -> str:
    """A project's id from its name or id; exits 1 if there's none."""
    for p in client.get("/v1/projects", params={"status": "all"}).json().get("items", []):
        if project in (p.get("project_id"), p.get("name")):
            return p["project_id"]
    console.print(f"[red]Project not found: {escape(project)}[/red]")
    raise typer.Exit(1)


def _scope(client: LumiverbClient, library: str | None, project: str | None) -> dict:
    if library and project:
        console.print("[red]Give a library or a project, not both.[/red]")
        raise typer.Exit(2)
    if library:
        return {"library_id": library_id_for(client, library)}
    if project:
        return {"project_id": _project_id_for(client, project)}
    return {}


def _where(scope: dict) -> str:
    return f" in {scope['name']}" if scope.get("name") else ""


def _clips_of(n: int) -> str:
    """'3 clips'' or '1 clip's', for "N clips' descriptions"."""
    return f"{n:,} clip's" if n == 1 else f"{n:,} clips'"


def _n(value: int) -> str:
    return f"{value:,}" if value else "-"


@producers_app.callback()
def producers_list(
    ctx: typer.Context,
    library: Annotated[str | None, typer.Option("--library", help="Count one library (name or id).")] = None,
    project: Annotated[str | None, typer.Option("--project", help="Count one project (name or id).")] = None,
) -> None:
    """Each producer's counts: current, missing, stale and failing, and its upgrades."""
    if ctx.invoked_subcommand is not None:
        return
    client = LumiverbClient()
    params = _scope(client, library, project)
    producers = client.get("/v1/producers", params=params).json().get("producers", [])
    table = Table(show_header=True, header_style="bold")
    for col in ("Producer", "Current", "Missing", "Stale", "Failing", "Upgrading"):
        table.add_column(col, justify="left" if col in ("Producer", "Upgrading") else "right")
    notes = []
    for p in producers:
        c = p.get("counts") or {}
        ups = p.get("upgrades", [])
        table.add_row(f"{escape(p['title'])} [dim]({p['artifact']})[/dim]", _n(c.get("current", 0)),
                      _n(c.get("missing", 0)), _n(c.get("stale", 0)), _n(c.get("failing", 0)),
                      f"{sum(u['remaining'] for u in ups):,} left" if ups else "")
        for u in ups:
            notes.append(f"{p['title']}: upgrading{_where(u['scope']) or ' everything'}, {u['remaining']:,} left "
                         f"of {u['total']:,} ({u['edits']} edits; id {u['upgrade_id']}).")
            if u.get("still_stale"):
                notes.append(f"  {u['still_stale']:,} were made again but are still stale (settings changed "
                             "while they were made); they aren't handed out again.")
        if p.get("edited"):
            notes.append(f"{p['title']}: {p['edited']:,} of the stale have your edits.")
        if c.get("stale") and not p.get("upgradable", True):
            notes.append(f"{p['title']} can't be upgraded yet: {p.get('why_not') or ''}")
    console.print(table)
    for note in notes:
        console.print(escape(note))
    console.print("[dim]Stale ones were made with an older producer or settings: "
                  "lumiverb producers upgrade <producer> makes them again.[/dim]")


def _error(r) -> tuple[str | None, str, dict]:
    """(code, message, details) of a refusal."""
    try:
        body = r.json() or {}
    except ValueError:
        body = {}
    err = body.get("error") or {}
    return err.get("code"), err.get("message") or body.get("detail") or r.text, err.get("details") or {}


@producers_app.command("upgrade")
def producers_upgrade(
    artifact: Annotated[str, typer.Argument(help="The producer, as the listing names it (e.g. vision, ocr, clip).")],
    library: Annotated[str | None, typer.Option("--library", help="Only this library (name or id).")] = None,
    project: Annotated[str | None, typer.Option("--project", help="Only this project (name or id).")] = None,
    edits: Annotated[str | None, typer.Option(
        "--edits", help="Clips with your edits: keep (on top of the new one), replace (kept in history) or skip.",
    )] = None,
    redo_everything: Annotated[bool, typer.Option(
        "--redo-everything", help="For CLIP and faces, which go all at once: confirm the count.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask; fail instead.")] = False,
) -> None:
    """Make a producer's stale artifacts again (admins). Takes the clips stale now;
    the worker makes them after anything missing."""
    if edits is not None and edits not in EDIT_CHOICES:
        console.print("[red]--edits is keep, replace or skip.[/red]")
        raise typer.Exit(2)
    client = LumiverbClient()
    body: dict = _scope(client, library, project)
    if edits:
        body["edits"] = edits
    if redo_everything:
        body["confirm"] = True
    while True:
        r = client.raw("POST", f"/v1/producers/{artifact}/upgrade", json=dict(body))
        if r.status_code < 400:
            break
        code, message, details = _error(r)
        if r.status_code == 409 and code == "edited_clips" and "edits" not in body:
            console.print(f"[yellow]{escape(message)}[/yellow]")
            if yes:
                console.print("[red]Add --edits keep, --edits replace or --edits skip to go ahead.[/red]")
                raise typer.Exit(2)
            console.print("  keep: the new one is made underneath; your edits stay on top.")
            console.print("  replace: as each is made again, your edits go to history and the new one shows.")
            console.print("  skip: clips with your edits stay as they are.")
            body["edits"] = typer.prompt("Keep, replace or skip", default="keep",
                                         type=click.Choice(list(EDIT_CHOICES), case_sensitive=False)).lower()
            continue
        if r.status_code == 409 and code == "redo_everything" and not body.get("confirm"):
            console.print(f"[yellow]{escape(message)}[/yellow]")
            if yes:
                console.print("[red]Add --redo-everything to go ahead.[/red]")
                raise typer.Exit(2)
            if not typer.confirm(f"Redo all {int(details.get('stale', 0)):,}?", default=False):
                console.print("Nothing changed.")
                return
            body["confirm"] = True
            continue
        console.print(f"[red]Couldn't upgrade: {escape(message)}[/red]")
        raise typer.Exit(1)
    data = r.json()
    if data.get("upgrading"):
        console.print(f"Upgrading {_clips_of(data['upgrading'])} {escape(artifact)}{escape(_where(data['scope']))}: "
                      "the worker makes them again after anything missing.")
    else:
        console.print("Nothing to upgrade: every stale clip has your edits.")
    if data.get("skipped_edited"):
        console.print(f"Skipped {clips(data['skipped_edited'])} with your edits.")
    if data.get("edits_to_replace"):
        console.print(f"Your edits on {clips(data['edits_to_replace'])} move to history as each is made again.")


@producers_app.command("cancel")
def producers_cancel(
    artifact: Annotated[str, typer.Argument(help="The producer whose upgrades stop.")],
    upgrade: Annotated[str | None, typer.Option("--upgrade", help="Only this upgrade (its id).")] = None,
) -> None:
    """Stop upgrading (admins): what isn't made again yet stays stale."""
    client = LumiverbClient()
    r = client.raw("DELETE", f"/v1/producers/{artifact}/upgrade", params={"upgrade_id": upgrade} if upgrade else {})
    if r.status_code >= 400:
        console.print(f"[red]Couldn't cancel: {escape(_error(r)[1])}[/red]")
        raise typer.Exit(1)
    console.print("Stopped. What isn't made again yet will stay stale.")
