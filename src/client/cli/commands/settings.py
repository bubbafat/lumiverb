"""`lumiverb settings`: account-wide settings (the web's Settings → Playback and Files)."""

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


def _public(signed_in: int | None, public: int | None) -> int | None:
    """What public pages get: never more than signed-in people."""
    caps = [c for c in (signed_in, public) if c is not None]
    return min(caps) if caps else None


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
    public = _public(settings.get("video_preview_max_seconds"), settings.get("public_video_preview_max_seconds"))
    console.print(f"On public pages: {_describe(public)}")
    console.print(f"Follow moves and renames: {_on_off(settings.get('follow_moves', True))}")
    console.print(f"Trash: {_trash(settings.get('trash_days', 30))}")


def _trash(days: int | None) -> str:
    return "emptied by hand only" if days is None else f"deleted for good after {days} {'day' if days == 1 else 'days'}"


def _on_off(value: object) -> str:
    return "off" if value is False else "on"


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


@settings_app.command("follow-moves")
def follow_moves(state: Annotated[str, typer.Argument(help="'on' or 'off'")]) -> None:
    """Whether the same content is the same asset (admins only). On: a moved or
    renamed file keeps its notes, ratings and projects, and so does the copy left
    when the original is deleted. Off: a file at a new path is a new asset."""
    choice = state.strip().lower()
    if choice not in ("on", "off"):
        console.print("[red]Give 'on' or 'off'.[/red]")
        raise typer.Exit(2)
    settings = LumiverbClient().patch("/v1/tenant/settings", json={"follow_moves": choice == "on"}).json()
    console.print(f"Follow moves and renames: {_on_off(settings.get('follow_moves', True))}")
    if settings.get("follow_moves") is False:
        console.print("A file at a new path is now a new asset; nothing is matched by content.")


MAX_TRASH_DAYS = 3650


@settings_app.command("trash-days")
def trash_days(days: Annotated[str, typer.Argument(help="Days, or 'off' to empty the trash by hand only")]) -> None:
    """How long clips, libraries and projects stay in the trash before they're
    deleted for good: 30 until changed (admins only). Archived clips are never deleted."""
    text = days.strip().lower()
    if text == "off":
        value = None
    elif re.fullmatch(r"[0-9]+", text) and 1 <= int(text) <= MAX_TRASH_DAYS:
        value = int(text)
    else:
        console.print(f"[red]Give 'off' or a whole number of days from 1 to {MAX_TRASH_DAYS:,}.[/red]")
        raise typer.Exit(2)
    settings = LumiverbClient().patch("/v1/tenant/settings", json={"trash_days": value}).json()
    console.print(f"Trash: {_trash(settings.get('trash_days'))}")
