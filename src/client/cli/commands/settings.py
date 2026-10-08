"""`lumiverb settings`: account-wide settings (the web's Settings → Playback)."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console

from src.client.cli.client import LumiverbClient

settings_app = typer.Typer(help="Account-wide settings. Anyone can view them; admins change them.")
console = Console()

MAX_SECONDS = 86_400


def _describe(cap: int | None) -> str:
    return "whole video" if cap is None else f"first {cap} seconds"


@settings_app.command("show")
def show() -> None:
    """Show account-wide settings."""
    settings = LumiverbClient().get("/v1/tenant/settings").json()
    console.print(f"Video playback: {_describe(settings.get('video_preview_max_seconds'))}")


@settings_app.command("video-preview")
def video_preview(
    length: Annotated[str, typer.Argument(help="'full' for the whole video, or the most seconds to play")],
) -> None:
    """How much of each video plays, for everyone and on public pages (admins only)."""
    if length.strip().lower() == "full":
        value: int | None = None
    elif length.strip().isdigit() and 1 <= int(length) <= MAX_SECONDS:
        value = int(length)
    else:
        console.print(f"[red]Give 'full' or a whole number of seconds from 1 to {MAX_SECONDS:,}.[/red]")
        raise typer.Exit(2)
    settings = LumiverbClient().patch("/v1/tenant/settings", json={"video_preview_max_seconds": value}).json()
    console.print(f"Video playback: {_describe(settings.get('video_preview_max_seconds'))}")
