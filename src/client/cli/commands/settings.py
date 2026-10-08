"""`lumiverb settings`: account-wide settings (the web's Settings → Playback)."""

from __future__ import annotations

import re
from typing import Annotated

import typer
from rich.console import Console

from src.client.cli.client import LumiverbClient

settings_app = typer.Typer(help="Account-wide settings. Anyone can view them; admins change them.")
console = Console()

MAX_SECONDS = 86_400
_LENGTH_HELP = "'full' for the whole video, or the most seconds to play"


def _describe(cap: int | None) -> str:
    return "whole video" if cap is None else f"first {cap} seconds"


def _length(text: str) -> int | None:
    """'full' → None, or whole seconds 1..86,400 (ASCII digits only); exits 2 otherwise."""
    text = text.strip()
    if text.lower() == "full":
        return None
    if re.fullmatch(r"[0-9]+", text) and 1 <= int(text) <= MAX_SECONDS:
        return int(text)
    console.print(f"[red]Give 'full' or a whole number of seconds from 1 to {MAX_SECONDS:,}.[/red]")
    raise typer.Exit(2)


@settings_app.command("show")
def show() -> None:
    """Show account-wide settings."""
    settings = LumiverbClient().get("/v1/tenant/settings").json()
    console.print(f"Video playback: {_describe(settings.get('video_preview_max_seconds'))}")
    console.print(f"On public pages: {_describe(settings.get('public_video_preview_max_seconds'))}")


@settings_app.command("video-preview")
def video_preview(length: Annotated[str, typer.Argument(help=_LENGTH_HELP)]) -> None:
    """How much of each video plays for signed-in people (admins only)."""
    value = _length(length)
    settings = LumiverbClient().patch("/v1/tenant/settings", json={"video_preview_max_seconds": value}).json()
    console.print(f"Video playback: {_describe(settings.get('video_preview_max_seconds'))}")


@settings_app.command("public-preview")
def public_preview(length: Annotated[str, typer.Argument(help=_LENGTH_HELP)]) -> None:
    """How much of each video public pages play: 10 seconds until set, never more than signed in (admins only)."""
    value = _length(length)
    settings = LumiverbClient().patch(
        "/v1/tenant/settings", json={"public_video_preview_max_seconds": value},
    ).json()
    console.print(f"On public pages: {_describe(settings.get('public_video_preview_max_seconds'))}")
