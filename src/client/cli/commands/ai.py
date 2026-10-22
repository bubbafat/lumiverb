"""lumiverb ai: the account's AI machines and the model each AI job uses,
as Settings › AI has them, for a server with no screen (an account's
admins). A machine's key is asked for at a prompt, never on the command
line, and never shown: the server keeps it write-only."""

from __future__ import annotations

from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.client.cli.client import LumiverbClient
from src.client.cli.decisions import api_error, remake_or_not
from src.client.cli.hidden import key_typed

console = Console()
ai_app = typer.Typer(help="AI machines and each AI job's model (Settings › AI). Account admins change them.")
machines_app = typer.Typer(help="The machines that do the AI jobs: add, edit, remove, and what each offers.")
ai_app.add_typer(machines_app, name="machines")


def _settings(client: LumiverbClient) -> dict:
    return client.get("/v1/ai").json()


def _machine(settings: dict, name: str) -> dict:
    """The machine by its name (any case), or exit 1 naming those there are."""
    found = [m for m in settings["machines"] if m["name"].casefold() == name.casefold()]
    if not found:
        names = ", ".join(m["name"] for m in settings["machines"]) or "none"
        console.print(f"[red]There's no machine called {escape(name)} (there are: {escape(names)}).[/red]")
        raise typer.Exit(1)
    return found[0]


def _jobs(settings: dict, given: str) -> list[str]:
    """Jobs as typed, comma-separated, each by its name (lumiverb ai list): exit 2 for one there isn't."""
    known = {j["job"]: j["label"] for j in settings["jobs"]}
    jobs = [j.strip() for j in given.split(",") if j.strip()]
    unknown = [j for j in jobs if j not in known]
    if unknown:
        console.print(f"[red]No job called {escape(', '.join(unknown))}: "
                      f"{escape(', '.join(f'{j} ({label})' for j, label in known.items()))}.[/red]")
        raise typer.Exit(2)
    return jobs


def _key(name: str) -> str:
    key = key_typed(f"Key for {name}")
    if not key:
        console.print("[red]No key given.[/red]")
        raise typer.Exit(1)
    return key


def _status(m: dict) -> str:
    status = m.get("status")
    if m["built_in"]:
        return "this server"
    if not m["enabled"]:
        return "off"
    if status is None:
        return "not checked yet"
    return "online" if status["online"] else f"offline: {status.get('error') or ''}"


def _said(r: httpx.Response, failed: str) -> None:
    """What went wrong, then exit 1: a machine's models when it doesn't offer what's asked."""
    err = api_error(r)
    console.print(f"[red]{escape(failed)}: {escape(err.get('message') or r.text)}[/red]")
    details = err.get("details") or {}
    if err.get("code") == "model_not_offered":
        for m in details.get("machines") or []:
            offers = ", ".join(m.get("models") or []) or m.get("error") or "nothing"
            console.print(f"  {escape(m.get('name', ''))}: {escape(offers)}")
        if details.get("models"):
            console.print(f"  It offers: {escape(', '.join(details['models']))}")
    raise typer.Exit(1)


def _machines_table(settings: dict) -> Table:
    table = Table(title="AI machines")
    for col in ("NAME", "URL", "JOBS", "AT ONCE", "KEY", "STATUS"):
        table.add_column(col)
    for m in settings["machines"]:
        key = "" if m["built_in"] else ("enter it again" if m["has_key"] and not m.get("key_readable", True)
                                        else "set" if m["has_key"] else "none")
        table.add_row(escape(m["name"]), escape(m["api_url"] or "—"), escape(", ".join(m["jobs"]) or "—"),
                      f"{m['at_once']:d}", key, escape(_status(m)))
    return table


@ai_app.command("list")
def ai_list() -> None:
    """The machines, and each job's model: what Settings › AI shows."""
    settings = _settings(LumiverbClient())
    console.print(_machines_table(settings))
    table = Table(title="Jobs")
    for col in ("JOB", "WHAT", "MODEL", "MACHINES OFFERING IT"):
        table.add_column(col)
    for j in settings["jobs"]:
        table.add_row(escape(j["job"]), escape(j["label"]), escape(j["model"] or "off"),
                      f"{j['offering']:d} of {j['machines']:d}" if j["machines"] else "none does it")
    console.print(table)


@machines_app.command("list")
def machines_list() -> None:
    """Each machine: where it is, its jobs, how many requests at once, whether a key is set, and its status."""
    console.print(_machines_table(_settings(LumiverbClient())))


@machines_app.command("models")
def machines_models(
    machine: Annotated[str, typer.Argument(help="A machine's name, or a URL to ask (Connect).")],
    key: Annotated[bool, typer.Option("--key", help="Ask for a key to ask a URL with (not shown).")] = False,
) -> None:
    """What a machine offers: its models, asked now (Settings › AI's Connect). Account admins."""
    client = LumiverbClient()
    if machine.startswith(("http://", "https://")):
        body = {"api_url": machine, "api_key": _key(machine) if key else ""}
    else:
        m = _machine(_settings(client), machine)
        body = {"api_url": m["api_url"], "machine_id": m["machine_id"], **({"api_key": _key(m["name"])} if key else {})}
    r = client.raw("POST", "/v1/ai/connect", json=body)
    if r.status_code >= 400:
        _said(r, "Couldn't ask it")
    for model in r.json()["models"]:
        console.print(escape(model))


@machines_app.command("test")
def machines_test(
    name: Annotated[str, typer.Argument(help="The machine's name.")],
    model: Annotated[str, typer.Option("--model", help="A model to try instead of the job's.")] = "",
) -> None:
    """Try a machine before it gets the work: it's sent what descriptions & text's work sends (a picture Lumiverb
    draws and a video's scenes, with this account's settings), and each answer is said, with the tokens it used of
    its room, or why it wouldn't do. Account admins."""
    client = LumiverbClient()
    m = _machine(_settings(client), name)
    r = client.raw("POST", f"/v1/ai/machines/{m['machine_id']}/try", json={"model": model})
    if r.status_code >= 400:
        _said(r, "Couldn't test it")
    tried = r.json()
    console.print(f"Sent {escape(tried['sent'])}, with {escape(tried['model'])}.")
    bad = False
    for t in tried["tries"]:
        used = f" · {t['tokens']['answer']:,} of {t['limit']:,} tokens" if t.get("tokens") else ""
        said = (f"[yellow]{escape(t['problem'])}[/yellow]" if t["problem"]
                else "[green]answered as the work needs it[/green]")
        console.print(f"\n[bold]{escape(', '.join(t['titles']))}[/bold]: {said} · {t['seconds']:.1f} s{used}")
        if t["answer"]:
            console.print(escape(t["answer"]), soft_wrap=True)
        bad |= bool(t["problem"])
    raise typer.Exit(1 if bad else 0)


@machines_app.command("add")
def machines_add(
    name: Annotated[str, typer.Argument(help="What to call it (OpenAI, Gaming PC).")],
    url: Annotated[str, typer.Option("--url", help="Its API, OpenAI-compatible: https://api.openai.com/v1, "
                                                   "http://192.168.1.20:11434/v1 for Ollama.")],
    jobs: Annotated[str, typer.Option("--jobs", help="The jobs it does, comma-separated (lumiverb ai list).")],
    key: Annotated[bool, typer.Option("--key", help="Ask for its key (not shown; or pipe it in).")] = False,
    at_once: Annotated[int, typer.Option("--at-once", min=1, max=32, help="Requests it takes at once.")] = 2,
    shares_gpu: Annotated[bool, typer.Option(
        "--shares-gpu", help="It runs on the GPU this server decodes video on: fewer requests while it does.")] = False,
    off: Annotated[bool, typer.Option("--off", help="Add it turned off.")] = False,
) -> None:
    """Add a machine. It's asked for its models first: it must offer each of its jobs' models. Account admins."""
    client = LumiverbClient()
    body = {"name": name, "api_url": url, "jobs": _jobs(_settings(client), jobs), "at_once": at_once,
            "enabled": not off, "shares_gpu": shares_gpu, "api_key": _key(name) if key else ""}
    r = client.raw("POST", "/v1/ai/machines", json=body)
    if r.status_code >= 400:
        _said(r, "Couldn't add it")
    console.print(f"[green]{escape(name)} added.[/green]")


@machines_app.command("edit")
def machines_edit(
    name: Annotated[str, typer.Argument(help="The machine's name.")],
    new_name: Annotated[str | None, typer.Option("--name", help="A new name.")] = None,
    url: Annotated[str | None, typer.Option("--url", help="Where it is now.")] = None,
    key: Annotated[bool, typer.Option("--key", help="Ask for a new key (not shown; or pipe it in).")] = False,
    no_key: Annotated[bool, typer.Option("--no-key", help="It needs no key: forget the one saved.")] = False,
    jobs: Annotated[str | None, typer.Option("--jobs", help="The jobs it does now, comma-separated.")] = None,
    at_once: Annotated[int | None, typer.Option("--at-once", min=1, max=32)] = None,
    on: Annotated[bool | None, typer.Option("--on/--off", help="Turn it on or off.", show_default=False)] = None,
    shares_gpu: Annotated[bool | None, typer.Option("--shares-gpu/--own-gpu", show_default=False)] = None,
) -> None:
    """Change a machine: only what's given. A new URL, key or job, or turning it on, asks it again. Its key never
    goes along to a new URL: give it again with --key. Account admins."""
    if key and no_key:
        console.print("[red]--key or --no-key, not both.[/red]")
        raise typer.Exit(2)
    client = LumiverbClient()
    settings = _settings(client)
    m = _machine(settings, name)
    body: dict = {k: v for k, v in {"name": new_name, "api_url": url, "at_once": at_once, "enabled": on,
                                    "shares_gpu": shares_gpu}.items() if v is not None}
    if jobs is not None:
        body["jobs"] = _jobs(settings, jobs)
    if key:
        body["api_key"] = _key(m["name"])
    elif no_key:
        body["api_key"] = ""
    if not body:
        console.print("[yellow]Nothing to change: give what to change (lumiverb ai machines edit --help).[/yellow]")
        raise typer.Exit(2)
    r = client.raw("PATCH", f"/v1/ai/machines/{m['machine_id']}", json=body)
    if r.status_code >= 400:
        _said(r, "Couldn't change it")
    now = _machine(r.json(), new_name or m["name"])
    console.print(f"[green]{escape(now['name'])} changed.[/green]")
    if url is not None and m["has_key"] and not now["has_key"]:
        console.print("[yellow]Its key wasn't sent to the new URL: give it again with --key if it needs one.[/yellow]")


@machines_app.command("remove")
def machines_remove(
    name: Annotated[str, typer.Argument(help="The machine's name.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask.")] = False,
) -> None:
    """Remove a machine, and its key with it. The last one doing a job too: the job's work waits. Account admins."""
    client = LumiverbClient()
    m = _machine(_settings(client), name)
    if not yes and not typer.confirm(f"Remove {m['name']}, and its key?", default=False):
        console.print("Nothing changed.")
        raise typer.Exit(0)
    r = client.raw("DELETE", f"/v1/ai/machines/{m['machine_id']}")
    if r.status_code >= 400:
        _said(r, "Couldn't remove it")
    console.print(f"[green]{escape(m['name'])} removed.[/green]")


@ai_app.command("model")
def ai_model(
    job: Annotated[str, typer.Argument(help="The job (lumiverb ai list).")],
    model: Annotated[str | None, typer.Argument(help="The model, as its machines list it.")] = None,
    off: Annotated[bool, typer.Option("--off", help="Turn the job off: no model.")] = False,
    remake: Annotated[bool, typer.Option(
        "--remake", help="Don't ask: remake what the old model made too, after everything else.")] = False,
    new_only: Annotated[bool, typer.Option(
        "--new-only", help="Don't ask: new files only; what the old model made stays as it is.")] = False,
) -> None:
    """A job's model, or set it (account admins): a machine doing the job must offer it. What the old model made is
    then made another way than now: the server asks whether to remake it too, after everything else, or make only
    new files with the new model (ADR-018 decision 6), and so does this, unless --remake or --new-only says."""
    if remake and new_only:
        console.print("[red]--remake or --new-only, not both.[/red]")
        raise typer.Exit(2)
    if off and model:
        console.print("[red]A model or --off, not both.[/red]")
        raise typer.Exit(2)
    client = LumiverbClient()
    settings = _settings(client)
    if len(_jobs(settings, job)) != 1:
        console.print("[red]One job at a time.[/red]")
        raise typer.Exit(2)
    if model is None and not off:
        j = next(j for j in settings["jobs"] if j["job"] == job)
        console.print(f"{escape(j['label'])}: {escape(j['model'] or 'off')}")
        if j["choices"]:
            console.print(f"Its machines offer: {escape(', '.join(j['choices']))}")
        return
    answer = {"redo": True} if remake else {"redo": False} if new_only else {}
    body = {"model": "" if off else model, **answer}
    r = client.raw("PUT", f"/v1/ai/jobs/{job}", json=body)
    if r.status_code == 409 and api_error(r).get("code") == "redo_on_change":
        console.print(f"[yellow]{escape(api_error(r).get('message') or '')}[/yellow]")
        redo = remake_or_not((api_error(r).get("details") or {}).get("question"))
        if redo is None:
            console.print("Nothing changed.")
            raise typer.Exit(0)
        r = client.raw("PUT", f"/v1/ai/jobs/{job}", json={**body, "redo": redo})
    if r.status_code >= 400:
        _said(r, "Couldn't change it")
    console.print(f"[green]{escape(job)}: {escape(model or 'off')}.[/green]")
