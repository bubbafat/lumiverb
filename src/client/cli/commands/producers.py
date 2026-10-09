"""`lumiverb producers`: what each producer has made, and stopping or resuming its redo.

Mirrors the web's Settings → Processing (ADR-016 phase 4). An artifact is
current, missing, stale (made with another producer, model or settings
than now) or failing. Stale ones are made again after anything missing:
changing a setting was the approval (Robert, Oct 9). An admin can stop a
producer's redo and resume it; a new model for it resumes it.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.client.cli.client import LumiverbClient
from src.client.cli.commands.archive import library_id_for

producers_app = typer.Typer(
    help="What each producer has made (current, missing, stale, failing), and stopping or resuming its redo.",
    invoke_without_command=True,
)
console = Console()


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


def _n(value: int) -> str:
    return f"{value:,}" if value else "-"


def _redo(p: dict) -> str:
    """What happens to its stale clips."""
    stale = (p.get("counts") or {}).get("stale", 0)
    if not stale:
        return ""
    if not p.get("redoable", True):
        return "not yet"
    return "stopped" if p.get("paused") else "redoing"


@producers_app.callback()
def producers_list(
    ctx: typer.Context,
    library: Annotated[str | None, typer.Option("--library", help="Count one library (name or id).")] = None,
    project: Annotated[str | None, typer.Option("--project", help="Count one project (name or id).")] = None,
) -> None:
    """Each producer's counts: current, missing, stale and failing, and whether its stale ones are being redone."""
    if ctx.invoked_subcommand is not None:
        return
    client = LumiverbClient()
    params = _scope(client, library, project)
    producers = client.get("/v1/producers", params=params).json().get("producers", [])
    table = Table(show_header=True, header_style="bold")
    for col in ("Producer", "Current", "Missing", "Stale", "Failing", "Redo"):
        table.add_column(col, justify="left" if col in ("Producer", "Redo") else "right")
    notes = []
    for p in producers:
        c = p.get("counts") or {}
        table.add_row(f"{escape(p['title'])} [dim]({p['artifact']})[/dim]", _n(c.get("current", 0)),
                      _n(c.get("missing", 0)), _n(c.get("stale", 0)), _n(c.get("failing", 0)), _redo(p))
        if c.get("stale") and not p.get("redoable", True):
            notes.append(f"{p['title']} isn't made again yet: {p.get('why_not') or ''}")
        elif p.get("paused"):
            notes.append(f"{p['title']}: redo stopped; lumiverb producers resume {p['artifact']} carries on.")
    console.print(table)
    for note in notes:
        console.print(escape(note))
    console.print("[dim]Stale ones were made with another model or settings than now; they're made again "
                  "after anything missing.[/dim]")


def _error(r) -> str:
    try:
        body = r.json() or {}
    except ValueError:
        body = {}
    return (body.get("error") or {}).get("message") or body.get("detail") or r.text


@producers_app.command("stop")
def producers_stop(
    artifact: Annotated[str, typer.Argument(help="The producer, as the listing names it (e.g. vision, ocr, clip).")],
) -> None:
    """Stop redoing a producer's stale clips (admins). What's missing is still made."""
    r = LumiverbClient().raw("POST", f"/v1/producers/{artifact}/redo/stop")
    if r.status_code >= 400:
        console.print(f"[red]Couldn't stop: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    console.print(f"Stopped redoing {escape(artifact)}: its stale clips stay as they are until you resume it "
                  "(or its model changes).")


@producers_app.command("resume")
def producers_resume(
    artifact: Annotated[str, typer.Argument(help="The producer whose redo carries on.")],
) -> None:
    """Redo a producer's stale clips again (admins), after anything missing."""
    r = LumiverbClient().raw("POST", f"/v1/producers/{artifact}/redo/resume")
    if r.status_code >= 400:
        console.print(f"[red]Couldn't resume: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    console.print(f"Redoing {escape(artifact)} again, after anything missing.")
