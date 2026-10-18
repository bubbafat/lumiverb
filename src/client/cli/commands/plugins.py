"""lumiverb plugins: the plug-ins an account turns on or off (ADR-021), and
what turning one on asks first (its acknowledgment: shown in full, then
accepted or not)."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.client.cli.client import LumiverbClient
from src.client.cli.decisions import send_until_decided

console = Console()
plugins_app = typer.Typer(help="Plug-ins: optional parts of Lumiverb an account turns on.")

STATES = {"on": "on", "off": "off", "waiting": "off: its terms changed, accept them again"}


@plugins_app.command("list")
def plugins_list() -> None:
    """Every plug-in and whether it's on in this account."""
    plugins = LumiverbClient().get("/v1/plugins").json()["plugins"]
    table = Table(title="Plug-ins")
    table.add_column("ID")
    table.add_column("PLUG-IN")
    table.add_column("STATE")
    table.add_column("ACCEPTED")
    for p in plugins:
        accepted = p.get("acknowledged")
        table.add_row(p["plugin_id"], p["title"], STATES.get(p["state"], p["state"]),
                      f"version {accepted['version']}, {accepted['at'][:10]}" + (f", {accepted['by']}"
                                                                                 if accepted.get("by") else "")
                      if accepted else "")
    console.print(table)


def _show(details: dict) -> None:
    console.print(f"\n[bold]{escape(details.get('title', ''))}[/bold]\n")
    for paragraph in str(details.get("text", "")).split("\n\n"):
        console.print(escape(paragraph) + "\n")
    for link in details.get("links", []):
        console.print(f"  {escape(link['label'])}: {escape(link['url'])}")
    console.print()


@plugins_app.command("enable")
def plugins_enable(
    plugin_id: Annotated[str, typer.Argument(help="The plug-in's id (lumiverb plugins list).")],
    accept: Annotated[bool, typer.Option(
        "--accept", help="Accept what turning it on asks, without being asked (it's still shown).")] = False,
) -> None:
    """Turn a plug-in on. One that asks an acknowledgment (a license, say) shows it in full first.
    Account admins."""

    def acknowledge(details: dict) -> dict:
        _show(details)
        if not accept and not typer.confirm("Accept it and turn the plug-in on?", default=False):
            console.print("Left off.")
            raise typer.Exit(0)
        return {"acknowledged": details.get("version")}

    client = LumiverbClient()
    r = send_until_decided(client, "PUT", f"/v1/plugins/{plugin_id}", {"enabled": True},
                           answers={"acknowledgment_required": acknowledge}, failed="Couldn't turn it on")
    console.print(f"[green]{escape(r.json()['title'])} is on.[/green]")


@plugins_app.command("disable")
def plugins_disable(
    plugin_id: Annotated[str, typer.Argument(help="The plug-in's id (lumiverb plugins list).")],
) -> None:
    """Turn a plug-in off. Account admins."""
    client = LumiverbClient()
    r = send_until_decided(client, "PUT", f"/v1/plugins/{plugin_id}", {"enabled": False}, answers={},
                           failed="Couldn't turn it off")
    console.print(f"[green]{escape(r.json()['title'])} is off.[/green]")
