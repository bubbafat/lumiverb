"""What a person types for a producer or a pause switch (ADR-024 phase 1):
its name, with or without `lumiverb.` (`image.description`). An old word
(`vision`) is refused since part 5, saying the name to type.

It's matched against what the server lists, and sent as the server lists it.
What the CLI shows of a producer is its name, without `lumiverb.` (`shown`).
"""

from __future__ import annotations

from collections.abc import Iterable

import typer
from rich.console import Console
from rich.markup import escape

from src.producers.names import short, typed

console = Console()


def shown(value: str) -> str:
    """A producer or switch the server answered, as a person types it."""
    return short(value)


def producers_listed(client) -> list[str]:
    """The producers the server lists, as it spells them (GET /v1/producers)."""
    return [p["artifact"] for p in client.get("/v1/producers", params={"counts": "false"}).json()
            .get("producers", [])]


def switches_listed(client) -> list[str]:
    """The pause switches the server lists, as it spells them (the queue's), with `all`; none when it can't say."""
    try:
        queue = client.get("/v1/producers/queue").json() or {}
    except Exception:  # noqa: BLE001 — the names are a help; what's typed is then sent as a name
        return []
    return ["all", *(sw["target"] for sw in queue.get("switches") or [])]


def resolved(word: str, listed: Iterable[str], what: str = "producer", *, server_says: bool = False) -> str:
    """What's typed, as the server lists it; exits 2 saying the name to type for an old word, and naming what
    there is when nothing listed is called that, unless server_says: then it's sent by its name (or as typed)
    for the server to say why it isn't one (a pause of what scans make, say)."""
    listed = list(listed)
    found, renamed = typed(word, listed)
    if renamed:
        console.print(f"[red]{escape(renamed)}[/red]")
        raise typer.Exit(2)
    if found is None and server_says:
        from src.producers.names import PREFIX, makers

        found = PREFIX + word if PREFIX + word in makers() else word
    if found is None:
        console.print(f"[red]No {what} is called {escape(word)}: one of "
                      f"{', '.join(escape(shown(x)) for x in listed)}.[/red]")
        raise typer.Exit(2)
    return found
