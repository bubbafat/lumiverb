"""Whether a device has used HTTPS yet (Settings › Network: other devices'
plain HTTP is turned off only once one has, routers/system.py).

Every request passes here. One that came over HTTPS from another device is
noted, the first time since HTTPS last changed (lifecycle/network.py
saw_https: a stat after that). nginx says how it came (X-Forwarded-Proto,
which it sets on every request it passes on), and only nginx's counts:
uvicorn takes it from a connection from this machine (its proxy headers,
FORWARDED_ALLOW_IPS), making the request https and its client the device;
called otherwise (tests), a connection from this machine's counts the same
way, as for the address it saw (client_address.py). Only where Lumiverb
runs the web server (NGINX_CONFIG: a Mac's) is it noted.
"""

from __future__ import annotations

import logging
from pathlib import Path

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from src.server.api.client_address import client_address, is_loopback
from src.server.config import get_settings
from src.server.lifecycle import network

logger = logging.getLogger(__name__)
_said = False


def note(request: Request) -> None:
    global _said
    settings = get_settings()
    if not settings.nginx_config:
        return
    peer = request.client.host if request.client else ""
    said = is_loopback(peer) and request.headers.get("x-forwarded-proto") == "https"
    if request.scope.get("scheme") != "https" and not said:
        return  # plain HTTP, or not through nginx: whatever it says
    address = client_address(request)
    if is_loopback(address):
        return
    try:
        network.saw_https(Path(settings.data_dir), address)
    except OSError as exc:
        if not _said:  # once: it's tried again on every such request
            logger.warning("Couldn't note a device using HTTPS: %s", exc)
            _said = True


class NoteHttps:
    """Notes, then passes every request on as it is."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            note(Request(scope))
        await self.app(scope, receive, send)
