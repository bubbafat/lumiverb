#!/usr/bin/env python3
"""Bulk-delete all people and/or dismissed clusters via the Lumiverb API.

Face embeddings and detections are preserved — only the `people` rows and
their `face_person_matches` are deleted.  After running, visit the cluster
UI to re-confirm clusters from scratch; ongoing face detection/embedding
will continue in the background.

Uses the existing API endpoints — no server changes required:
    GET  /v1/people            — list non-dismissed people (paginated)
    GET  /v1/people/dismissed  — list dismissed people (paginated)
    DELETE /v1/people/{id}     — delete a person and their face matches

Auth comes from the CLI config (same as `lumiverb` commands).  Pass
`--base-url` and `--token` to override.

Examples:
    # Dry run — enumerate only, delete nothing
    uv run python scripts/reset-face-clusters.py --dry-run

    # Delete everything (people + dismissed), skip confirmation
    uv run python scripts/reset-face-clusters.py --yes

    # Only delete dismissed clusters (the usual "clean the junk drawer")
    uv run python scripts/reset-face-clusters.py --only dismissed --yes

    # Only delete active people
    uv run python scripts/reset-face-clusters.py --only people --yes
"""

from __future__ import annotations

import argparse
import sys
from typing import Literal

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)

from src.client.cli.client import LumiverbAPIError, LumiverbClient

Kind = Literal["people", "dismissed"]

PAGE_LIMIT = 100  # server cap

console = Console()


def _list_all(
    client: LumiverbClient, path: str, label: str
) -> list[tuple[str, str, int]]:
    """Page through a people list endpoint and return [(person_id, display_name, face_count), ...].

    Fetches the entire list up front so the subsequent delete loop does not
    interact with cursor pagination (deletions shift the page window).
    """
    items: list[tuple[str, str, int]] = []
    after: str | None = None
    page = 0
    while True:
        page += 1
        params: dict[str, object] = {"limit": PAGE_LIMIT}
        if after:
            params["after"] = after
        resp = client.get(path, params=params)
        data = resp.json()
        page_items = data.get("items", [])
        for it in page_items:
            items.append(
                (
                    it["person_id"],
                    it.get("display_name") or "(unnamed)",
                    it.get("face_count", 0),
                )
            )
        console.print(
            f"  [dim]{label} page {page}: +{len(page_items)} (total {len(items)})[/dim]"
        )
        next_cursor = data.get("next_cursor")
        if not next_cursor or not page_items:
            break
        after = next_cursor
    return items


def _delete_all(
    client: LumiverbClient,
    items: list[tuple[str, str, int]],
    label: str,
) -> tuple[int, int]:
    """Delete each person in `items`; return (ok_count, fail_count)."""
    ok = 0
    fail = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]deleting {task.fields[label]}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            "delete", total=len(items), label=label
        )
        for person_id, display_name, face_count in items:
            try:
                client.delete(f"/v1/people/{person_id}")
                ok += 1
            except LumiverbAPIError as e:
                fail += 1
                console.print(
                    f"  [red]\u2717[/red] {person_id} ({display_name}, {face_count} faces): "
                    f"{e.message}"
                )
            progress.update(task, advance=1)
    return ok, fail


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bulk-delete all people and/or dismissed clusters via the "
            "Lumiverb API. Face embeddings are preserved."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="API base URL (overrides CLI config)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="API token (overrides CLI config)",
    )
    parser.add_argument(
        "--only",
        choices=("people", "dismissed", "all"),
        default="all",
        help="Limit to active people, dismissed clusters, or both (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Enumerate and print what would be deleted; do not delete.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip interactive confirmation before deleting.",
    )
    args = parser.parse_args()

    kinds: list[tuple[Kind, str, str]] = []
    if args.only in ("all", "people"):
        kinds.append(("people", "/v1/people", "active people"))
    if args.only in ("all", "dismissed"):
        kinds.append(("dismissed", "/v1/people/dismissed", "dismissed clusters"))

    with LumiverbClient(base_url=args.base_url, token=args.token) as client:
        console.print(f"[bold]API:[/bold] {client.base_url}")
        if not client.token:
            console.print(
                "[red]No API token configured.[/red] Set it via "
                "`lumiverb auth login`, or pass --token."
            )
            return 2

        # --- Enumerate ---
        console.print()
        console.print("[bold]Enumerating…[/bold]")
        collected: dict[Kind, list[tuple[str, str, int]]] = {}
        total_faces_affected = 0
        for kind, path, label in kinds:
            try:
                items = _list_all(client, path, label)
            except LumiverbAPIError as e:
                console.print(f"[red]Failed to list {label}:[/red] {e.message}")
                return 1
            collected[kind] = items
            total_faces = sum(fc for _, _, fc in items)
            total_faces_affected += total_faces
            console.print(
                f"  [green]\u2713[/green] {label}: "
                f"{len(items)} clusters, {total_faces} face assignments"
            )

        total_people = sum(len(v) for v in collected.values())
        if total_people == 0:
            console.print()
            console.print("[yellow]Nothing to delete.[/yellow]")
            return 0

        console.print()
        console.print(
            f"[bold]Will delete:[/bold] {total_people} clusters, "
            f"unlinking {total_faces_affected} face assignments "
            f"(face detections and embeddings are preserved)."
        )

        if args.dry_run:
            console.print()
            console.print("[bold]Dry run — would delete:[/bold]")
            for kind, items in collected.items():
                console.print(f"  [cyan]{kind}:[/cyan]")
                for person_id, display_name, face_count in items[:20]:
                    console.print(
                        f"    {person_id}  [dim]{display_name}[/dim]  "
                        f"({face_count} faces)"
                    )
                if len(items) > 20:
                    console.print(f"    [dim]… and {len(items) - 20} more[/dim]")
            return 0

        if not args.yes:
            console.print()
            answer = input("Type 'yes' to proceed: ").strip().lower()
            if answer != "yes":
                console.print("[yellow]Aborted.[/yellow]")
                return 1

        # --- Delete ---
        console.print()
        total_ok = 0
        total_fail = 0
        for kind, _path, label in kinds:
            items = collected[kind]
            if not items:
                continue
            ok, fail = _delete_all(client, items, label)
            total_ok += ok
            total_fail += fail
            console.print(
                f"  [green]\u2713[/green] {label}: {ok} deleted, {fail} failed"
            )

        console.print()
        if total_fail:
            console.print(
                f"[yellow]Done with errors:[/yellow] {total_ok} deleted, "
                f"{total_fail} failed."
            )
            return 1

        console.print(
            f"[bold green]Done:[/bold green] {total_ok} clusters deleted. "
            "Visit the cluster UI to re-confirm clusters."
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
