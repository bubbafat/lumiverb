"""Who sent a request: its address as nginx saw it.

The API listens on this machine only, behind nginx, which appends the
address it saw to X-Forwarded-For ($proxy_add_x_forwarded_for). So the
right-most entry is the real one, and anything left of it is whatever the
client sent. That header counts only on a connection from this machine (our
own proxy's): one from anywhere else is its own address, whatever it claims.
"""

from __future__ import annotations

import ipaddress

from starlette.requests import Request


def is_loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped  # ::ffff:127.0.0.1
    return address.is_loopback


def client_address(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and is_loopback(peer):
        return forwarded.split(",")[-1].strip() or peer
    return peer


def on_this_machine(request: Request) -> bool:
    """The request comes from the machine Lumiverb runs on (through nginx or not)."""
    return is_loopback(client_address(request))
