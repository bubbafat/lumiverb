"""`lumiverb pause|resume <name>`: the pause switches (Robert, Oct 9).

One switch per processing action: each producer the scheduler makes,
`scans` and `upkeep`; `all` is every one. Without a name nothing happens
and the names are listed. After acting it says the account's state, derived
from the switches: Running, Partly paused or Paused.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from src.client.cli.client import LumiverbClient

console = Console()

STATE_WORDS = {"running": "Running", "partly": "Partly paused", "paused": "Paused"}


def _error(r) -> str:
    try:
        body = r.json() or {}
    except ValueError:
        body = {}
    return (body.get("error") or {}).get("message") or r.text


def _switches(client: LumiverbClient) -> dict:
    try:
        return client.get("/v1/producers/queue").json() or {}
    except Exception:  # noqa: BLE001 — the names are a help; acting doesn't depend on them
        return {}


def _usage(client: LumiverbClient, command: str) -> None:
    names = [sw["target"] for sw in _switches(client).get("switches") or []]
    console.print(f"Usage: lumiverb {command} <name>")
    console.print(f"Names: {', '.join(['all', *names]) if names else 'all, scans, upkeep or a producer'}")
    raise typer.Exit(2)


def _act(command: str, name: str | None) -> None:
    client = LumiverbClient()
    if not name:
        _usage(client, command)
    # "all" is a target like any other; a switch's work, never only its redo.
    r = client.raw("POST", f"/v1/producers/{name}/{command}", json={"scope": "work"})
    if r.status_code >= 400:
        console.print(f"[red]Couldn't {command} {escape(name or '')}: {escape(_error(r))}[/red]")
        raise typer.Exit(1)
    queue = _switches(client)
    console.print(f"Processing: {STATE_WORDS.get(queue.get('state', ''), 'Running')}")
    paused = [sw["title"] for sw in queue.get("switches") or [] if sw.get("paused")]
    if paused and queue.get("state") != "paused":
        console.print(f"Paused: {escape(', '.join(paused))}")


def register(app: typer.Typer) -> None:
    @app.command("pause")
    def pause(
        name: Annotated[str | None, typer.Argument(help="A producer (e.g. vision), scans, upkeep, or all.")] = None,
    ) -> None:
        """Pause one processing switch, or all (admins). What's running finishes."""
        _act("pause", name)

    @app.command("resume")
    def resume(
        name: Annotated[str | None, typer.Argument(help="A producer (e.g. vision), scans, upkeep, or all.")] = None,
    ) -> None:
        """Resume one processing switch, or all (admins)."""
        _act("resume", name)
