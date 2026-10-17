"""`lumiverb device run`: this machine as the reader of libraries on a
server elsewhere (ADR-018 phase 6; src/processing/device.py).

An account admin registers the device on the server (Settings, or POST
/v1/devices) and hands it the key, set here with `lumiverb config set
--api-url <server> --api-key -`; each library it should read names it
(PUT /v1/libraries/{id}/reader). Where a library's folder is on this
machine, when it isn't at the path the server keeps: `lumiverb config
map-root`.
"""

from __future__ import annotations

import logging
from typing import Annotated

import typer
from rich.console import Console

device_app = typer.Typer(help="Read libraries for a server elsewhere: scan them and make what needs their files.")
console = Console()


@device_app.command("run")
def run(
    once: Annotated[bool, typer.Option("--once", help="One pass, then stop (a scan of what's due, then the work).")]
    = False,
    full_scan_every_min: Annotated[float | None, typer.Option(
        "--full-scan-every-min", min=1, help="How often each library is scanned in full (this machine's setting if left out).")]
    = None,
    name: Annotated[str | None, typer.Option("--name", help="What its check-ins call it (this machine's name if left out).")]
    = None,
) -> None:
    """Check in, scan the libraries this device reads and make what needs
    their files (probe facts, capture, analysis proxies), until stopped
    (Ctrl-C finishes the clip in hand)."""
    from src.client.cli.client import LumiverbClient
    from src.processing.device import Device
    from src.processing.machine import current

    logging.getLogger("src").setLevel(logging.INFO)
    every = (full_scan_every_min or current().full_scan_every_min) * 60
    device = Device(LumiverbClient(), full_scan_every=every, name=name)
    if once:
        done = device.once()
        reading = ", ".join(lib["name"] for lib in device.libraries.values()) or "no library"
        console.print(f"Reads {reading}; went through {done} clip(s)")
        return
    console.print("Reading for the server until stopped (Ctrl-C)")
    try:
        device.run(say=console.print)
    except KeyboardInterrupt:
        device.stopping.set()
        console.print("Stopped")
