"""User management CLI commands: user create (an invite), user invite (a new
link), user list, user set-role, user remove."""

from __future__ import annotations

import re
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from src.client.cli.client import LumiverbClient
from src.client.cli.decisions import api_error

console = Console()

user_app = typer.Typer(help="Manage users.")

VALID_ROLES = ("admin", "editor", "viewer")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _find_user_by_email(client: LumiverbClient, email: str) -> dict:
    """Fetch all users and return the one matching email, or exit."""
    resp = client.get("/v1/users")
    users: list[dict] = resp.json()
    user = next((u for u in users if u.get("email") == email), None)
    if user is None:
        console.print(f"[red]Error: user not found: {email}[/red]")
        raise typer.Exit(1)
    return user


def _say_link(email: str, url: str) -> None:
    console.print(f"Send {escape(email)} this link. It works once, for 7 days, and they choose their password there:")
    print(url)  # on a line of its own, never wrapped: it's copied whole


@user_app.command("create")
def create_user(
    email: Annotated[str, typer.Option("--email", help="Email address for the new user.")],
    role: Annotated[str, typer.Option("--role", help="Role: admin, editor, or viewer.")] = "viewer",
) -> None:
    """Invite someone: they get a link to choose their own password."""
    if not _EMAIL_RE.match(email):
        console.print("[red]Error: invalid email address.[/red]")
        raise typer.Exit(1)

    if role not in VALID_ROLES:
        console.print(f"[red]Error: role must be one of: {', '.join(VALID_ROLES)}[/red]")
        raise typer.Exit(1)

    client = LumiverbClient()
    resp = client.raw("POST", "/v1/users", json={"email": email, "role": role})
    if resp.status_code == 409:
        console.print(f"[red]Error: email already registered: {escape(email)}[/red]")
        raise typer.Exit(1)
    if resp.status_code >= 400:
        err = api_error(resp)
        console.print(f"[red]Error: {escape(err.get('message') or resp.text or f'HTTP {resp.status_code}')}[/red]")
        raise typer.Exit(1)
    data = resp.json()
    console.print(f"[green]Invited {escape(data['email'])} ({data['role']})[/green]")
    _say_link(data["email"], data["invite_url"])


@user_app.command("invite")
def invite_again(
    email: Annotated[str, typer.Option("--email", help="Email of someone who hasn't signed in yet.")],
) -> None:
    """A new invite link for someone who hasn't signed in yet; their old one stops working."""
    client = LumiverbClient()
    user = _find_user_by_email(client, email)
    resp = client.raw("POST", f"/v1/users/{user['user_id']}/invite")
    if resp.status_code >= 400:
        err = api_error(resp)
        console.print(f"[red]Error: {escape(err.get('message') or resp.text or f'HTTP {resp.status_code}')}[/red]")
        raise typer.Exit(1)
    _say_link(email, resp.json()["invite_url"])


@user_app.command("list")
def list_users() -> None:
    """List all users for the current tenant."""
    client = LumiverbClient()
    resp = client.get("/v1/users")
    resp.raise_for_status()
    users: list[dict] = resp.json()

    table = Table()
    table.add_column("EMAIL")
    table.add_column("ROLE")
    table.add_column("LAST LOGIN")
    for u in users:
        last_login = u.get("last_login_at")
        if last_login:
            last_login = last_login[:16].replace("T", " ")
        else:
            last_login = "never"
        table.add_row(u.get("email", ""), u.get("role", ""), last_login)
    console.print(table)


@user_app.command("set-role")
def set_user_role(
    email: Annotated[str, typer.Option("--email", help="Email of the user to update.")],
    role: Annotated[str, typer.Option("--role", help="New role: admin, editor, or viewer.")],
) -> None:
    """Change a user's role."""
    if role not in VALID_ROLES:
        console.print(f"[red]Error: role must be one of: {', '.join(VALID_ROLES)}[/red]")
        raise typer.Exit(1)

    client = LumiverbClient()
    user = _find_user_by_email(client, email)
    user_id = user["user_id"]

    resp = client.patch(f"/v1/users/{user_id}", json={"role": role})
    if resp.status_code == 409:
        err = resp.json().get("error", {}).get("code", "")
        if err == "last_admin":
            console.print("[red]Error: cannot demote the last admin.[/red]")
        else:
            console.print(f"[red]Error: {resp.json().get('error', {}).get('message', 'conflict')}[/red]")
        raise typer.Exit(1)
    resp.raise_for_status()
    data = resp.json()
    console.print(f"[green]Role updated: {data['email']} → {data['role']}[/green]")


@user_app.command("remove")
def remove_user(
    email: Annotated[str, typer.Option("--email", help="Email of the user to remove.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Remove a user (prompts for confirmation unless --yes)."""
    client = LumiverbClient()
    user = _find_user_by_email(client, email)
    user_id = user["user_id"]

    if not yes and not typer.confirm(f"Remove {email}?", default=False):
        console.print("Aborted.")
        raise typer.Exit(0)

    resp = client.raw("DELETE", f"/v1/users/{user_id}")
    if resp.status_code >= 400:
        err = api_error(resp)
        if resp.status_code == 409 and err.get("code") == "last_admin":
            console.print("[red]Error: cannot remove the last admin.[/red]")
        else:
            console.print(f"[red]Error: {escape(err.get('message') or resp.text or f'HTTP {resp.status_code}')}[/red]")
        raise typer.Exit(1)
    console.print("[green]User removed.[/green]")
