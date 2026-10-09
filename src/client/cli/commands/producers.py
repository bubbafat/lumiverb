"""`lumiverb producers`: what each producer has made, pausing and resuming, and stopping or resuming a redo.

Mirrors the web's Settings → Processing (ADR-016 phase 4). An artifact is
current, missing, stale (made with another producer, model or settings
than now) or failing. Stale ones are made again after anything missing:
changing a setting was the approval (Robert, Oct 9). An admin can stop a
producer's redo and resume it; a new model for it resumes it. An admin can
also pause all processing, or one producer's, and resume it: nothing more
of it starts, and what's running finishes.
"""

from __future__ import annotations

import math
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.client.cli.client import LumiverbClient
from src.client.cli.commands.archive import library_id_for

producers_app = typer.Typer(
    help="What each producer has made (current, missing, stale, failing), pausing and resuming, and its redo.",
    invoke_without_command=True,
)
redo_app = typer.Typer(help="Stop or resume redoing a producer's stale clips (admins).")
producers_app.add_typer(redo_app, name="redo")
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
    return "stopped" if p.get("redo_stopped") else "redoing"


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
    queue = client.get("/v1/producers/queue").json() or {}
    if queue.get("paused"):
        since = str(queue.get("paused_at") or "")[:16].replace("T", " ")
        console.print(f"[yellow]All processing is paused{f' (since {since})' if since else ''}: nothing more "
                      "starts. lumiverb producers resume carries on.[/yellow]")
    table = Table(show_header=True, header_style="bold")
    for col in ("Producer", "Current", "Missing", "Stale", "Failing", "Redo"):
        table.add_column(col, justify="left" if col in ("Producer", "Redo") else "right")
    notes = []
    for p in producers:
        c = p.get("counts") or {}
        paused = " [yellow]paused[/yellow]" if p.get("paused") else ""
        table.add_row(f"{escape(p['title'])} [dim]({p['artifact']})[/dim]{paused}", _n(c.get("current", 0)),
                      _n(c.get("missing", 0)), _n(c.get("stale", 0)), _n(c.get("failing", 0)), _redo(p))
        if p.get("paused"):
            notes.append(f"{p['title']}: paused; lumiverb producers resume {p['artifact']} carries on.")
        if c.get("stale") and not p.get("redoable", True):
            notes.append(f"{p['title']} isn't made again yet: {p.get('why_not') or ''}")
        elif p.get("redo_stopped"):
            notes.append(f"{p['title']}: redo stopped; lumiverb producers redo resume {p['artifact']} carries on.")
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


@producers_app.command("pause")
def producers_pause(
    artifact: Annotated[str | None, typer.Argument(
        help="Only this producer (e.g. vision, transcript); without it, all processing.")] = None,
) -> None:
    """Pause all processing, or one producer's (admins): nothing more of it
    starts until it's resumed; what's running finishes."""
    r = LumiverbClient().raw("POST", f"/v1/producers/{artifact}/pause" if artifact else "/v1/producers/pause")
    if r.status_code >= 400:
        console.print(f"[red]Couldn't pause: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    if artifact:
        console.print(f"Paused {escape(artifact)}: nothing more of it starts until lumiverb producers resume "
                      f"{escape(artifact)}. What's running finishes.")
    else:
        console.print("Paused all processing: nothing more starts, scans included, until lumiverb producers "
                      "resume. What's running finishes.")


@producers_app.command("resume")
def producers_resume(
    artifact: Annotated[str | None, typer.Argument(
        help="Only this producer; without it, all processing.")] = None,
) -> None:
    """Carry on with all processing, or one paused producer (admins)."""
    r = LumiverbClient().raw("POST", f"/v1/producers/{artifact}/resume" if artifact else "/v1/producers/resume")
    if r.status_code >= 400:
        console.print(f"[red]Couldn't resume: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    if artifact:
        console.print(f"Resumed {escape(artifact)}.")
    else:
        console.print("Resumed all processing. Producers paused one by one stay paused.")


@redo_app.command("stop")
def redo_stop(
    artifact: Annotated[str, typer.Argument(help="The producer, as the listing names it (e.g. vision, ocr, clip).")],
) -> None:
    """Stop redoing a producer's stale clips (admins). What's missing is still made."""
    r = LumiverbClient().raw("POST", f"/v1/producers/{artifact}/redo/stop")
    if r.status_code >= 400:
        console.print(f"[red]Couldn't stop: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    console.print(f"Stopped redoing {escape(artifact)}: its stale clips stay as they are until you resume it "
                  "(or its model changes).")


@redo_app.command("resume")
def redo_resume(
    artifact: Annotated[str, typer.Argument(help="The producer whose redo carries on.")],
) -> None:
    """Redo a producer's stale clips again (admins), after anything missing."""
    r = LumiverbClient().raw("POST", f"/v1/producers/{artifact}/redo/resume")
    if r.status_code >= 400:
        console.print(f"[red]Couldn't resume: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    console.print(f"Redoing {escape(artifact)} again, after anything missing.")


@producers_app.command("failures")
def producers_failures(
    artifact: Annotated[str | None, typer.Argument(help="Only this producer's (e.g. vision, transcript).")] = None,
    library: Annotated[str | None, typer.Option("--library", help="Only this library (name or id).")] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many (newest failure first).")] = 50,
) -> None:
    """Clips whose last try failed: why, how many tries, when the next one is (or that it was given up)."""
    client = LumiverbClient()
    params: dict = {"limit": max(1, min(limit, 500))}
    if artifact:
        params["artifact"] = artifact
    if library:
        params["library_id"] = library_id_for(client, library)
    r = client.raw("GET", "/v1/producers/failures", params=params)
    if r.status_code >= 400:
        console.print(f"[red]Couldn't list failures: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    data = r.json()
    items = data.get("items", [])
    if not items:
        console.print("Nothing is failing.")
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("Clip", "Producer", "Tries", "Next try", "Error"):
        table.add_column(col, overflow="fold")
    for f in items:
        nxt = "given up" if f.get("given_up") else (f.get("retry_at") or "")[:16].replace("T", " ")
        table.add_row(f"{escape(f['rel_path'])} [dim]({escape(f['library_name'])})[/dim]", f["artifact"],
                      str(f.get("attempts", 0)), nxt, escape(f.get("error", ""))[:300])
    console.print(table)
    if data.get("next_cursor"):
        console.print(f"[dim]The {len(items)} most recent; --limit shows more.[/dim]")
    console.print("[dim]lumiverb producers retry tries them again.[/dim]")


@producers_app.command("retry")
def producers_retry(
    artifact: Annotated[str | None, typer.Argument(help="Only this producer's failing clips.")] = None,
    asset: Annotated[list[str] | None, typer.Option("--asset", help="Only this clip (repeatable).")] = None,
    library: Annotated[str | None, typer.Option("--library", help="Only this library (name or id).")] = None,
) -> None:
    """Try failing clips again now, given up or not (editors and admins); the back-off starts over."""
    client = LumiverbClient()
    body: dict = {}
    if artifact:
        body["artifact"] = artifact
    if asset:
        body["asset_ids"] = list(asset)
    if library:
        body["library_id"] = library_id_for(client, library)
    r = client.raw("POST", "/v1/producers/failures/retry", json=body)
    if r.status_code >= 400:
        console.print(f"[red]Couldn't try them again: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    n = int(r.json().get("retried", 0))
    console.print(f"{n:,} clip{'' if n == 1 else 's'} will be tried again shortly." if n else "Nothing was failing.")


def _bounds(f: dict) -> str:
    if f.get("fixed"):
        return "fixed"
    if f.get("minimum") is None:
        return f["kind"]
    unit = f" {f['unit']}" if f.get("unit") else ""
    return f"{f['minimum']:g}–{f['maximum']:g}{unit}"


def _shown(value: object) -> str:
    text = "—" if value in ("", None) else str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def _number(f: dict, raw: str) -> int | float:
    """A number setting as typed, or exit 2 saying what it must be. Its
    bounds are the server's to judge; NaN and infinity can't be sent at all."""
    kind = "whole number" if f["kind"] == "int" else "number"
    try:
        number = float(raw)
    except ValueError:
        number = math.nan
    if not math.isfinite(number) or (f["kind"] == "int" and not number.is_integer()):
        console.print(f"[red]{escape(f['label'])} is a finite {kind}.[/red]")
        raise typer.Exit(2)
    return int(number) if f["kind"] == "int" else number


@producers_app.command("settings")
def producers_settings(
    artifact: Annotated[str, typer.Argument(help="The producer, as the listing names it (e.g. transcript).")],
    changes: Annotated[list[str] | None, typer.Argument(
        help="KEY=VALUE to change (admins); KEY= puts it back to its default.")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask; make again what the old settings made.")] = False,
) -> None:
    """A producer's settings, and changing the ones its code reads (admins).
    New settings make what the old ones made again, after anything missing:
    the server asks first, and so does this."""
    client = LumiverbClient()
    producers = {p["artifact"]: p for p in client.get("/v1/producers", params={"counts": "false"}).json()
                 .get("producers", [])}
    p = producers.get(artifact)
    if p is None:
        console.print(f"[red]No producer {escape(artifact)}: one of {', '.join(producers)}.[/red]")
        raise typer.Exit(2)
    fields = {f["key"]: f for f in p.get("fields") or []}
    if not changes:
        table = Table(show_header=True, header_style="bold", title=f"{p['title']} · version {p['version']}")
        for col in ("Setting", "Key", "Now", "Default", "Can be"):
            table.add_column(col)
        for f in fields.values():
            table.add_row(escape(f["label"]), f["key"], escape(_shown(f["value"])), escape(_shown(f["default"])),
                          _bounds(f))
        console.print(table)
        for f in fields.values():
            if f.get("fixed") and f["key"] != "model":
                console.print(f"[dim]{escape(f['key'])}: {escape(f['fixed'])}[/dim]")
        for f in fields.values():  # text the table cut short, whole
            if f["kind"] == "text" and _shown(f["value"]) != str(f["value"]) and f["value"]:
                console.print(f"\n[bold]{escape(f['key'])}[/bold]\n{escape(str(f['value']))}")
        return
    body: dict = {}
    for change in changes:
        key, sep, raw = change.partition("=")
        f = fields.get(key)
        if not sep or f is None:
            console.print(f"[red]Give KEY=VALUE, with one of: {', '.join(fields)}.[/red]")
            raise typer.Exit(2)
        if raw == "":
            body[key] = None
        elif f["kind"] == "text":
            body[key] = raw
        else:
            body[key] = _number(f, raw)
    r = client.raw("PUT", f"/v1/producers/{artifact}/settings", json={"settings": body, "redo": yes})
    if r.status_code == 409 and ((r.json() or {}).get("error") or {}).get("code") == "redo_on_change":
        console.print(f"[yellow]{escape(_error(r))}[/yellow]")
        if not typer.confirm("Go ahead?", default=False):
            console.print("Nothing changed.")
            raise typer.Exit(0)
        r = client.raw("PUT", f"/v1/producers/{artifact}/settings", json={"settings": body, "redo": True})
    if r.status_code >= 400:
        console.print(f"[red]Couldn't change them: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    now = r.json().get("settings", {})
    console.print(", ".join(f"{escape(k)} = {escape(_shown(now.get(k)))}" for k in body))
