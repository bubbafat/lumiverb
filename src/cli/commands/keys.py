from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from src.cli.client import LumiverbClient


console = Console()
keys_app = typer.Typer(help="Manage API keys for the current tenant.")


def _human_relative(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return ts
    now = datetime.now(timezone.utc)
    delta = now - dt
    days = delta.days
    seconds = delta.seconds
    if days == 0:
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            minutes = seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''} ago"
    # Fallback to ISO date for older timestamps.
    return dt.date().isoformat()


@keys_app.command("list")
def keys_list() -> None:
    """List all non-revoked API keys for the current tenant."""
    client = LumiverbClient()
    resp = client.get("/v1/keys")
    data = resp.json()
    keys = data.get("keys", [])

    table = Table(title="API Keys")
    table.add_column("Key ID", style="dim")
    table.add_column("Label")
    table.add_column("Admin", justify="center")
    table.add_column("Last Used")
    table.add_column("Created")

    for k in keys:
        is_admin = bool(k.get("is_admin", False))
        admin_flag = "✓" if is_admin else ""
        last_used_raw = k.get("last_used_at")
        created_raw = k.get("created_at", "")
        table.add_row(
            k.get("key_id", ""),
            k.get("label") or "",
            admin_flag,
            _human_relative(last_used_raw),
            created_raw or "",
        )

    console.print(table)


@keys_app.command("create")
def keys_create(
    label: Annotated[str | None, typer.Option("--label", help="Optional human-readable label for the key.")] = None,
    admin: Annotated[bool, typer.Option("--admin", help="Create an admin key.")] = False,
) -> None:
    """Create a new API key for the current tenant."""
    client = LumiverbClient()
    resp = client.post(
        "/v1/keys",
        json={"label": label, "is_admin": admin},
    )
    data = resp.json()
    plaintext = data.get("plaintext", "")
    console.print(f"[green]Created key:[/green] {plaintext}")
    console.print("Copy this now — it will not be shown again.")

    table = Table(show_header=True)
    table.add_column("Key ID", style="dim")
    table.add_column("Label")
    table.add_column("Admin", justify="center")

    is_admin = bool(data.get("is_admin", False))
    admin_flag = "✓" if is_admin else ""
    table.add_row(
        data.get("key_id", ""),
        data.get("label") or "",
        admin_flag,
    )
    console.print(table)


@keys_app.command("revoke")
def keys_revoke(
    key_id: Annotated[str, typer.Option("--key-id", help="Key ID to revoke (e.g. key_01...).")],
) -> None:
    """Revoke an API key for the current tenant."""
    confirm = typer.confirm(f"Revoke key {key_id}? [y/N]", default=False)
    if not confirm:
        console.print("Aborted.")
        raise typer.Exit(0)

    client = LumiverbClient()
    resp = client.raw("DELETE", f"/v1/keys/{key_id}")
    if resp.status_code == 204:
        console.print(f"[green]Revoked {key_id}[/green]")
        return
    if resp.status_code == 409:
        try:
            data = resp.json()
            error = data.get("error", {})
            code = error.get("code")
        except Exception:
            code = None
        if code == "last_admin_key":
            console.print(
                "[red]Cannot revoke the last remaining admin key.[/red] "
                "Create another admin key first, then revoke this one."
            )
            raise typer.Exit(1)
    # Fallback to standard error handling.
    client._handle_response(resp)  # type: ignore[attr-defined]

