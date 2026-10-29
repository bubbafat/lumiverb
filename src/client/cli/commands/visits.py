"""`lumiverb visits`: what public pages' visitors did (ADR-025): every place's totals (an account admin's), or a
project's or a library's day by day, its clips most opened and where visitors came from. On a server whose admin
pages aren't reachable from elsewhere (a demo), run on the machine itself."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.client.cli.client import LumiverbClient

console = Console()

COLUMNS = (("view", "Views"), ("open", "Opened"), ("play", "Played"), ("export", "Exported"), ("demo", "Demos"))


def _said(counted: dict) -> str:
    """Times, and visitors when they differ: "12 (8 visitors)"."""
    times, visitors = counted["times"], counted["visitors"]
    if not times:
        return ""
    return f"{times:,}" if visitors in (0, times) else f"{times:,} ({visitors:,} {'visitor' if visitors == 1 else 'visitors'})"


def _used(rows: list[dict]) -> list[tuple[str, str]]:
    """The columns any row has a count in (views always)."""
    return [(k, name) for k, name in COLUMNS if k == "view" or any(r[k]["times"] for r in rows)]


def visits(
    days: Annotated[int, typer.Option("--days", help="The days to report, today and those before it.")] = 30,
    project: Annotated[str | None, typer.Option("--project", help="A project's id: its own, day by day.")] = None,
    library: Annotated[str | None, typer.Option("--library", help="A public library's id: its own, day by day.")] = None,
) -> None:
    """What public pages' visitors did: pages seen, clips opened, videos played; projects exported; demos started.
    Counted on the server, nobody's address kept. A visitor is one each day."""
    if project and library:
        console.print("[red]Give --project or --library, not both.[/red]")
        raise typer.Exit(2)
    client = LumiverbClient()
    if not project and not library:
        data = client.get("/v1/visits", params={"days": days}).json()
        places = data["places"]
        if not places:
            console.print(f"No visits since {escape(data['since'])}.")
            return
        columns = _used(places)
        table = Table(title=f"Visits since {escape(data['since'])}")
        table.add_column("Place")
        table.add_column("Id", no_wrap=True)
        for _, name in columns:
            table.add_column(name, justify="right")
        table.add_column("Bots", justify="right")
        for p in places:
            name = "Demo" if p["place"] == "account" else (p["name"] or "(deleted)")
            table.add_row(escape(f"{p['place'].title()}: {name}"), escape(p["place_id"]),
                          *(_said(p[k]) for k, _ in columns), f"{p['bots']:,}" if p["bots"] else "")
        console.print(table)
        return

    path = f"/v1/projects/{project}/visits" if project else f"/v1/libraries/{library}/visits"
    data = client.get(path, params={"days": days}).json()
    columns = _used([data["totals"], *data["days"]])
    table = Table(title=f"Visits since {escape(data['since'])}")
    table.add_column("Day", no_wrap=True)
    for _, name in columns:
        table.add_column(name, justify="right")
    for d in data["days"]:
        table.add_row(escape(d["day"]), *(_said(d[k]) for k, _ in columns))
    table.add_row("Total", *(_said(data["totals"][k]) for k, _ in columns), end_section=True)
    console.print(table)
    if data["clips"]:
        clips_table = Table(title="Clips most opened and played")
        clips_table.add_column("Clip", no_wrap=True)
        clips_table.add_column("Opened", justify="right")
        clips_table.add_column("Played", justify="right")
        for c in data["clips"]:
            clips_table.add_row(escape(c["asset_id"]), f"{c['opened']:,}" if c["opened"] else "",
                                f"{c['played']:,}" if c["played"] else "")
        console.print(clips_table)
    if data["sources"]:
        console.print("Came from: " + escape(", ".join(f"{s['site']} ({s['visitors']:,})" for s in data["sources"])))
    if data["bots"]:
        console.print(f"Bots: {data['bots']:,} (not counted as visitors)")
