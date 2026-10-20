"""`lumiverb pause|resume <name>`: the pause switches (Robert, Oct 9).

One switch per processing action: each producer the scheduler makes and
`upkeep`; `all` is every one, and every library's scans. `scans` is each
library's own (ADR-018 phase 1): every library's, or one with --library.
A producer is named by what it makes, with or without `lumiverb.`
(`image.description`), or by its old word, warned of (src/client/cli/words.py).
Without a name nothing happens and the names are listed. After acting it
says the account's state, derived from the switches and the libraries'
scans: Running, Partly paused or Paused.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from src.client.cli.client import LumiverbClient
from src.client.cli.words import resolved, shown, switches_listed

console = Console()

STATE_WORDS = {"running": "Running", "partly": "Partly paused", "paused": "Paused"}


def _error(r) -> str:
    from src.client.cli.decisions import api_error

    return api_error(r).get("message") or r.text


def _switches(client: LumiverbClient) -> dict:
    try:
        return client.get("/v1/producers/queue").json() or {}
    except Exception:  # noqa: BLE001 — the names are a help; acting doesn't depend on them
        return {}


def _usage(client: LumiverbClient, command: str) -> None:
    names = [shown(sw["target"]) for sw in _switches(client).get("switches") or []]
    console.print(f"Usage: lumiverb {command} <name> (scans: --library NAME for one library's)")
    console.print(f"Names: {', '.join(['all', 'scans', *names]) if names else 'all, scans, upkeep or a producer'}")
    raise typer.Exit(2)


def _library_id(client: LumiverbClient, name: str) -> str:
    match = next((lib for lib in client.get("/v1/libraries").json() if name in (lib.get("name"), lib["library_id"])),
                 None)
    if match is None:
        console.print(f"[red]Library not found: {escape(name)}[/red]")
        raise typer.Exit(1)
    return match["library_id"]


def _act(command: str, name: str | None, library: str | None = None) -> None:
    client = LumiverbClient()
    if not name:
        _usage(client, command)
    if library and name != "scans":
        console.print("[red]--library goes with scans: a library's own Scans pause.[/red]")
        raise typer.Exit(2)
    if name == "scans":
        # Each library's own: the one named, or every one (named, never inferred).
        body = {"library_ids": [_library_id(client, library)]} if library else {"all": True}
        r = client.raw("POST", f"/v1/libraries/scans/{command}", json=body)
    else:
        # "all" is a target like any other; a switch's work, never only its redo. Sent as the server lists it
        # (when it can't say, by the name of what each makes, which every server from v0.1.105 takes).
        from src.producers.names import name_of
        from src.shared.producers import pause_targets

        listed = switches_listed(client) or ["all", *(name_of(a) for a in pause_targets())]
        name = resolved(name, listed, "switch", server_says=True)
        r = client.raw("POST", f"/v1/producers/{name}/{command}", json={"scope": "work"})
    if r.status_code >= 400:
        what = f"{name} of {library}" if library else shown(name or "")
        console.print(f"[red]Couldn't {command} {escape(what)}: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    queue = _switches(client)
    console.print(f"Processing: {STATE_WORDS.get(queue.get('state', ''), 'Running')}")
    paused = [sw["title"] for sw in queue.get("switches") or [] if sw.get("paused")]
    paused += [f"Scans of {lib['name']}" for lib in queue.get("libraries") or [] if lib.get("paused")]
    if paused and queue.get("state") != "paused":
        console.print(f"Paused: {escape(', '.join(paused))}")


def register(app: typer.Typer) -> None:
    @app.command("pause")
    def pause(
        name: Annotated[str | None, typer.Argument(
            help="A producer (e.g. image.description), scans, upkeep, or all.")] = None,
        library: Annotated[str | None, typer.Option(help="With scans: this library's only (its name or id).")] = None,
    ) -> None:
        """Pause one processing switch, or all (admins). What's running finishes."""
        _act("pause", name, library)

    @app.command("resume")
    def resume(
        name: Annotated[str | None, typer.Argument(
            help="A producer (e.g. image.description), scans, upkeep, or all.")] = None,
        library: Annotated[str | None, typer.Option(help="With scans: this library's only (its name or id).")] = None,
    ) -> None:
        """Resume one processing switch, or all (admins)."""
        _act("resume", name, library)
