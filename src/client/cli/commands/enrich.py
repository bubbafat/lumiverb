"""`lumiverb enrich PRODUCER (--library NAME | --all) [--redo]`: ask the
scheduler to do work now (POST /v1/producers/run).

The scheduler on the brain does all processing (ADR-016): this asks it to
try a producer's failing clips again at once and look again now, or with
--redo to make again what it's made (re-detecting faces, say). The
producer and the libraries are named (a producer or all; --library or
--all), never inferred from what's missing (Robert, Oct 9). It goes as the
scheduler goes: by tier, within its pools, and what's paused waits.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from src.client.cli.client import LumiverbClient
from src.client.cli.commands.archive import library_id_for

console = Console()


def _error(r) -> str:
    try:
        body = r.json() or {}
    except ValueError:
        body = {}
    return (body.get("error") or {}).get("message") or r.text


def _said(p: dict, redo: bool) -> str:
    """What the scheduler has to do for a producer, in a few words."""
    parts = [f"{p['missing']:,} to make"] if p["missing"] else []
    if p["redo"] and (redo or not p.get("redo_stopped")):
        parts.append(f"{p['redo']:,} to make again")
    if p["retried"]:
        parts.append(f"{p['retried']:,} tried again")
    words = ", ".join(parts) or "nothing to do"
    if p.get("paused"):
        words += " (paused)"
    elif p.get("redo_stopped") and p["redo"]:
        words += " (redo stopped)"
    return f"{escape(p['title'])}: {words}"


def register(app: typer.Typer) -> None:
    @app.command("enrich")
    def enrich(
        producer: Annotated[str | None, typer.Argument(help="A producer (e.g. faces, transcript), or all.")] = None,
        library: Annotated[str | None, typer.Option("--library", "-l", help="A library (name or id).")] = None,
        every_library: Annotated[bool, typer.Option("--all", help="Every library.")] = False,
        redo: Annotated[bool, typer.Option("--redo", help="Make again what's made (admins).")] = False,
    ) -> None:
        """Ask the scheduler to do a producer's work now, in a library or all of them."""
        if not producer or (library is None) == (not every_library):
            console.print("Usage: lumiverb enrich PRODUCER|all (--library NAME | --all) [--redo]")
            raise typer.Exit(2)
        client = LumiverbClient()
        body = {"producer": producer, "scope": "redo" if redo else "new",
                **({"all": True} if every_library else {"ids": [library_id_for(client, library or "")]})}
        r = client.raw("POST", "/v1/producers/run", json=body)
        if r.status_code >= 400:
            console.print(f"[red]Couldn't ask: {escape(_error(r))}[/red]")
            raise typer.Exit(1)
        for p in r.json().get("producers", []):
            console.print(_said(p, redo))
