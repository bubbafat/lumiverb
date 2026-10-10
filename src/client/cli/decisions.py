"""Decisions the API requires (cursor-api.md): a request that would have a
surprising outcome gets a 409 naming it, and is sent again with the answer.

`send_until_decided` is that loop, once for every command: each 409 code the
command knows has an answer, which asks the person (or, with --yes, stops)
and returns the fields to send again with.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import httpx
import typer
from rich.console import Console
from rich.markup import escape

from src.client.cli.client import LumiverbClient

console = Console()

# Given a 409's details, the fields to send again with; raises typer.Exit to stop.
Answer = Callable[[dict], dict]


def api_error(r: httpx.Response) -> dict:
    """The error envelope's {"code", "message", "details"}, or {}."""
    try:
        return (r.json() or {}).get("error") or {}
    except ValueError:
        return {}


def send_until_decided(
    client: LumiverbClient,
    method: str,
    path: str,
    body: dict,
    *,
    answers: Mapping[str, Answer],
    failed: str,
) -> httpx.Response:
    """Send body; while the API answers 409 with a code in answers, add that
    answer's fields and send again. Anything else that isn't 2xx, or an answer
    that changes nothing (the API asking the same again), prints
    "<failed>: <message>" and exits 1."""
    while True:
        r = client.raw(method, path, json=body)
        if r.status_code < 400:
            return r
        err = api_error(r)
        answer = answers.get(err.get("code")) if r.status_code == 409 else None
        # An answer whose fields were already sent isn't asked again.
        already = getattr(answer, "sends", None)
        if answer is not None and not (already and all(body.get(k) == v for k, v in already.items())):
            more = answer(err.get("details") or {})
            if any(body.get(k) != v for k, v in more.items()):
                body = {**body, **more}
                continue
        console.print(f"[red]{failed}: {escape(err.get('message') or r.text)}[/red]")
        raise typer.Exit(1)


def projects_say_yes(details: dict, *, what: str, yes: bool) -> bool:
    """The API asked about clips that projects use (409 in_projects): show which
    projects, then ask. With --yes there's no one to ask: say how to go ahead."""
    from src.client.cli.commands.archive import clips

    n = int(details.get("assets_in_projects", 0))
    console.print(f"[yellow]{clips(n)} {'is' if n == 1 else 'are'} in projects; {what}:[/yellow]")
    for p in details.get("projects", []):
        notes = ", ".join(x for x in ("archived" if p.get("status") == "archived" else "",
                                      "in the trash" if p.get("in_trash") else "") if x)
        console.print(f"  {escape(p.get('name', ''))}: {p.get('clips', 0)}" + (f" ({notes})" if notes else ""))
    if details.get("other_projects"):
        console.print(f"  and {details['other_projects']} more you can't see")
    if yes:
        console.print("[red]Add --remove-from-projects to go ahead.[/red]")
        return False
    return typer.confirm("Go ahead?", default=False)


def in_projects(*, what: str, yes: bool) -> Answer:
    """The answer to 409 in_projects: shows the projects and asks; yes sends
    remove_from_projects. No (or --yes, with no one to ask) stops: exit 0, or 2 with --yes."""

    def answer(details: dict) -> dict:
        if not projects_say_yes(details, what=what, yes=yes):
            raise typer.Exit(2 if yes else 0)
        return {"remove_from_projects": True}

    answer.sends = {"remove_from_projects": True}  # type: ignore[attr-defined]
    return answer
