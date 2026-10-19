"""`lumiverb producers`: what each producer has made, what's paused, and stopping or resuming a redo.

Mirrors the web's Settings → Processing (ADR-016 phase 4). An artifact is
current, missing, stale (made with another producer, model or settings
than now) or failing. Stale ones are made again after everything else
while the producer's redo runs: a new setting or model asks whether to
remake them too or make only new files (ADR-018 decision 6), which stops
its redo. An admin can stop a producer's redo and resume it. Pausing is
`lumiverb pause|resume` (commands/pausing.py); the listing says the state.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated

import click
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
    """What happens to its stale clips: redone, stopped, waiting for a pause to end, or not yet possible."""
    stale = (p.get("counts") or {}).get("stale", 0)
    if not stale:
        return ""
    if not p.get("redoable", True):
        return "not yet"
    if p.get("redo_stopped"):
        return "stopped"
    return "paused" if p.get("paused") else "redoing"


def _duration(seconds: float) -> str:
    """A span of time, roughly, as Settings → Processing says it."""
    if seconds < 60:
        return "under a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 10:
        return f"{hours} h {minutes % 60} min" if minutes % 60 else f"{hours} h"
    if hours < 48:
        return f"{round(minutes / 60)} h"
    days, rest = divmod(hours, 24)
    if days > 30:
        return "over a month"
    return f"{days} days {rest} h" if rest else f"{days} days"


def _caught_up(eta: dict) -> str:
    """The headline, as Settings → Processing says it: when everything is
    made, leaving out (and naming) what has no time."""
    why = {"no_machine": "no machine doing them now", "paused": "paused"}
    left = [f"{n['title'][:1].lower()}{n['title'][1:]} ({why.get(n.get('why', ''), 'not known yet')})"
            for n in eta.get("not_counted") or []]
    caught_up = eta.get("caught_up")
    if caught_up is None:
        whys = [n.get("why") for n in eta.get("not_counted") or []]
        if whys and all(w == "paused" for w in whys):
            return "Paused."
        return "How long until everything is made isn't known yet: it's learned from the jobs as they finish."
    if caught_up == 0 and not left:
        return "Caught up: everything is made."
    except_ = f", not counting {', '.join(left[:-1])}{' and ' if len(left) > 1 else ''}{left[-1]}" if left else ""
    if caught_up == 0:
        return f"Caught up{except_}."
    span = "under a minute" if caught_up < 60 else _duration(caught_up)
    return f"Caught up in {span if span in ('under a minute', 'over a month') else 'about ' + span}{except_}."


# What --compare holds equal, of each producer GET /v1/producers?counts=true lists.
COMPARED = ("counts", "settings", "settings_hash", "paused", "redo_stopped", "new_version")


def _by_name(answer: dict) -> dict[str, dict]:
    """A saved GET /v1/producers answer's producers by name (ADR-024: what each was called before, by RENAMED;
    an installed plug-in's work by its plug-in's id), each with what --compare holds equal."""
    from src.producers.names import ARTIFACTS

    def name(artifact: str) -> str:
        return ARTIFACTS.get(artifact) or artifact.removeprefix("plugin_")

    return {name(p["artifact"]): {k: p.get(k) for k in COMPARED} for p in answer.get("producers", [])}


def compare(before: dict, after: dict) -> list[str]:
    """What changed from one saved GET /v1/producers?counts=true answer to the other, by name: each producer's
    counts, settings, pause, stopped redo and new-version hold. Empty when nothing did."""
    was, now = _by_name(before), _by_name(after)
    out = [f"{n}: {'gone' if n not in now else 'new'}" for n in sorted(set(was) ^ set(now))]
    for n in sorted(set(was) & set(now)):
        out += [f"{n} {k}: {was[n][k]} → {now[n][k]}" for k in COMPARED if was[n][k] != now[n][k]]
    return out


@producers_app.callback()
def producers_list(
    ctx: typer.Context,
    library: Annotated[str | None, typer.Option("--library", help="Count one library (name or id).")] = None,
    project: Annotated[str | None, typer.Option("--project", help="Count one project (name or id).")] = None,
    compare_files: Annotated[tuple[Path, Path] | None, typer.Option(
        "--compare", help="Compare two saved GET /v1/producers?counts=true answers, before and after an upgrade:"
        " each producer's counts, settings, pauses and holds, by name (exits 1 when anything changed)")] = None,
) -> None:
    """Each producer's counts: current, missing, stale and failing, and whether its stale ones are being redone."""
    if ctx.invoked_subcommand is not None:
        return
    if compare_files is not None:
        import json

        changed = compare(*(json.loads(f.read_text()) for f in compare_files))
        for line in changed:
            console.print(escape(line))
        console.print("Nothing changed." if not changed else f"[red]{len(changed)} changed.[/red]")
        raise typer.Exit(1 if changed else 0)
    client = LumiverbClient()
    params = _scope(client, library, project)
    producers = client.get("/v1/producers", params=params).json().get("producers", [])
    queue = client.get("/v1/producers/queue").json() or {}
    from src.client.cli.commands.pausing import STATE_WORDS

    console.print(f"Processing: {STATE_WORDS.get(queue.get('state', ''), 'Running')}")
    for sw in queue.get("switches") or []:
        if sw.get("target") in ("scans", "upkeep"):
            console.print(f"{sw['title']}: {'[yellow]Paused[/yellow]' if sw.get('paused') else 'Running'}")
    # How long is left is the whole account's: only with its counts, and while the scheduler runs.
    eta = (queue.get("eta") or {}) if queue.get("live") and not params else {}
    left = eta.get("producers") or {}
    paused_left = {n.get("artifact") for n in eta.get("not_counted") or [] if n.get("why") == "paused"}
    table = Table(show_header=True, header_style="bold")
    columns = ("Producer", "Current", "Missing", "Stale", "Failing", "Redo", *(("Left",) if eta else ()))
    for col in columns:
        table.add_column(col, justify="left" if col in ("Producer", "Redo", "Left") else "right")
    notes = []
    for p in producers:
        c = p.get("counts") or {}
        paused = " [yellow]paused[/yellow]" if p.get("paused") else ""
        took = left.get(p["artifact"])
        held = p["artifact"] in paused_left
        table.add_row(f"{escape(p['title'])} [dim]({p['artifact']})[/dim]{paused}", _n(c.get("current", 0)),
                      _n(c.get("missing", 0)), _n(c.get("stale", 0)), _n(c.get("failing", 0)), _redo(p),
                      *(("Paused" if held else _duration(took) if took else "",) if eta else ()))
        if p.get("paused"):
            notes.append(f"{p['title']}: paused; lumiverb resume {p['artifact']} carries on.")
        if c.get("below"):
            notes.append(f"{p['title']}: {c['below']:,} made below the detail now (not counted as current).")
        if c.get("stale") and not p.get("redoable", True):
            notes.append(f"{p['title']} isn't made again yet: {p.get('why_not') or ''}")
        elif p.get("redo_stopped"):
            notes.append(f"{p['title']}: redo stopped; lumiverb producers redo resume {p['artifact']} carries on.")
    console.print(table)
    if eta:
        console.print(escape(_caught_up(eta)))
    for note in notes:
        console.print(escape(note))
    console.print("[dim]Stale ones were made with another model or settings than now; they're made again "
                  "after anything missing.[/dim]")


def _error(r) -> str:
    try:
        body = r.json() or {}
    except ValueError:
        body = {}
    return (body.get("error") or {}).get("message") or r.text


@redo_app.command("stop")
def redo_stop(
    artifact: Annotated[str, typer.Argument(help="The producer, as the listing names it (e.g. vision, ocr, clip).")],
) -> None:
    """Stop redoing a producer's stale clips (admins). What's missing is still made."""
    r = LumiverbClient().raw("POST", f"/v1/producers/{artifact}/pause", json={"scope": "redo"})
    if r.status_code >= 400:
        console.print(f"[red]Couldn't stop: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    console.print(f"Stopped redoing {escape(artifact)}: its stale clips stay as they are until lumiverb producers "
                  f"redo resume {escape(artifact)} (or its model or settings change).")


@redo_app.command("resume")
def redo_resume(
    artifact: Annotated[str, typer.Argument(help="The producer whose redo carries on.")],
) -> None:
    """Redo a producer's stale clips again (admins), after anything missing."""
    r = LumiverbClient().raw("POST", f"/v1/producers/{artifact}/resume", json={"scope": "redo"})
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
    console.print("[dim]lumiverb producers retry PRODUCER|all --all tries them again.[/dim]")


@producers_app.command("retry")
def producers_retry(
    artifact: Annotated[str | None, typer.Argument(help="A producer (e.g. vision), or all.")] = None,
    asset: Annotated[list[str] | None, typer.Option("--asset", help="This clip (repeatable).")] = None,
    library: Annotated[str | None, typer.Option("--library", help="A library (name or id).")] = None,
    every_library: Annotated[bool, typer.Option("--all", help="Every library.")] = False,
) -> None:
    """Try failing clips again now, given up or not (editors and admins); the back-off starts over.
    Name the producer (or all) and the clips: --library, --all or --asset."""
    if not artifact or (library is not None) + every_library + bool(asset) != 1:
        console.print("Usage: lumiverb producers retry PRODUCER|all (--library NAME | --all | --asset ID...)")
        raise typer.Exit(2)
    client = LumiverbClient()
    body: dict = {} if artifact == "all" else {"artifact": artifact}
    if asset:
        body["asset_ids"] = list(asset)
    else:
        body["all"] = True
        if library is not None:
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
    if f.get("kind") in ("level", "notch"):
        return " / ".join(f.get("choices") or [])
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


def _yes_no(f: dict, raw: str) -> bool:
    """A yes-or-no setting as typed (true/false, on/off, yes/no), or exit 2."""
    value = {"true": True, "on": True, "yes": True, "false": False, "off": False, "no": False}.get(raw.lower())
    if value is None:
        console.print(f"[red]{escape(f['label'])} is true or false.[/red]")
        raise typer.Exit(2)
    return value


def _remake_or_not(question: str | None) -> bool | None:
    """The answer to the server's question: remake what the old settings
    made too (True), new files only (False), or nothing changed (None). A
    question of something dropped (location guesses) is yes or no."""
    if question != "remake":
        return True if typer.confirm("Go ahead?", default=False) else None
    choice = typer.prompt("New files only (n), remake them too (r), or change nothing (c)?", default="n",
                          type=click.Choice(["n", "r", "c"]), show_choices=False)
    return {"n": False, "r": True}.get(choice)


@producers_app.command("settings")
def producers_settings(
    artifact: Annotated[str, typer.Argument(help="The producer, as the listing names it (e.g. transcript).")],
    changes: Annotated[list[str] | None, typer.Argument(
        help="KEY=VALUE to change (admins); KEY= puts it back to its default.")] = None,
    remake: Annotated[bool, typer.Option(
        "--remake", "--yes", "-y", help="Don't ask: remake what the old settings made too, after everything else.",
    )] = False,
    new_only: Annotated[bool, typer.Option(
        "--new-only", help="Don't ask: new files only; what the old settings made stays as it is.")] = False,
) -> None:
    """A producer's settings, and changing the ones its code reads (admins).
    What the old settings made is then made another way than now: the
    server asks whether to remake it too, after everything else, or make
    only new files with the new settings (ADR-018 decision 6), and so does
    this, unless --remake or --new-only says."""
    if remake and new_only:
        console.print("[red]--remake or --new-only, not both.[/red]")
        raise typer.Exit(2)
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
        elif f["kind"] in ("level", "notch"):
            if raw not in (f.get("choices") or []):
                console.print(f"[red]{escape(f['label'])} is one of {', '.join(f.get('choices') or [])}.[/red]")
                raise typer.Exit(2)
            body[key] = raw
        elif f["kind"] == "bool":
            body[key] = _yes_no(f, raw)
        else:
            body[key] = _number(f, raw)
    answer = {"redo": True} if remake else {"redo": False} if new_only else {}
    r = client.raw("PUT", f"/v1/producers/{artifact}/settings", json={"settings": body, **answer})
    if r.status_code == 409 and ((r.json() or {}).get("error") or {}).get("code") == "redo_on_change":
        console.print(f"[yellow]{escape(_error(r))}[/yellow]")
        redo = _remake_or_not(((r.json().get("error") or {}).get("details") or {}).get("question"))
        if redo is None:
            console.print("Nothing changed.")
            raise typer.Exit(0)
        r = client.raw("PUT", f"/v1/producers/{artifact}/settings", json={"settings": body, "redo": redo})
    if r.status_code >= 400:
        console.print(f"[red]Couldn't change them: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    saved = r.json()
    now = {**{f["key"]: f.get("value") for f in saved.get("fields") or []}, **saved.get("settings", {})}
    console.print(", ".join(f"{escape(k)} = {escape(_shown(now.get(k)))}" for k in body))
