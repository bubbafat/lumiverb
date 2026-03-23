"""User management CLI commands: create-user, list-users, set-user-role, remove-user."""

from __future__ import annotations

import re
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from src.cli.client import LumiverbClient

console = Console()

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


def register(app: typer.Typer) -> None:
    """Register user management commands onto the given Typer app."""

    @app.command("create-user")
    def create_user(
        email: Annotated[str, typer.Option("--email", help="Email address for the new user.")],
        role: Annotated[str, typer.Option("--role", help="Role: admin, editor, or viewer.")] = "viewer",
    ) -> None:
        """Create a new user with email and password."""
        if not _EMAIL_RE.match(email):
            console.print("[red]Error: invalid email address.[/red]")
            raise typer.Exit(1)

        if role not in VALID_ROLES:
            console.print(f"[red]Error: role must be one of: {', '.join(VALID_ROLES)}[/red]")
            raise typer.Exit(1)

        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
        if len(password) < 12:
            console.print("[red]Error: password must be at least 12 characters.[/red]")
            raise typer.Exit(1)

        client = LumiverbClient()
        resp = client.post("/v1/users", json={"email": email, "role": role, "password": password})
        if resp.status_code == 409:
            console.print(f"[red]Error: email already registered: {email}[/red]")
            raise typer.Exit(1)
        resp.raise_for_status()
        data = resp.json()
        console.print(f"[green]User created: {data['email']} ({data['role']})[/green]")

    @app.command("list-users")
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
                # Trim to YYYY-MM-DD HH:MM for compact display.
                last_login = last_login[:16].replace("T", " ")
            else:
                last_login = "never"
            table.add_row(u.get("email", ""), u.get("role", ""), last_login)
        console.print(table)

    @app.command("set-user-role")
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

    @app.command("remove-user")
    def remove_user(
        email: Annotated[str, typer.Option("--email", help="Email of the user to remove.")],
    ) -> None:
        """Remove a user (prompts for confirmation)."""
        client = LumiverbClient()
        user = _find_user_by_email(client, email)
        user_id = user["user_id"]

        confirm = typer.confirm(f"Remove {email}?", default=False)
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit(0)

        resp = client.delete(f"/v1/users/{user_id}")
        if resp.status_code == 409:
            err = resp.json().get("error", {}).get("code", "")
            if err == "last_admin":
                console.print("[red]Error: cannot remove the last admin.[/red]")
            else:
                console.print(f"[red]Error: {resp.json().get('error', {}).get('message', 'conflict')}[/red]")
            raise typer.Exit(1)
        if resp.status_code == 400:
            console.print(f"[red]Error: {resp.json().get('detail', 'bad request')}[/red]")
            raise typer.Exit(1)
        resp.raise_for_status()
        console.print("[green]User removed.[/green]")
