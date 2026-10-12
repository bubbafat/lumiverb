"""Who sent a request: its address as nginx saw it, and whether it comes
from the machine Lumiverb runs on.

The API listens on this machine only, behind nginx, which appends the
address it saw to X-Forwarded-For ($proxy_add_x_forwarded_for). So the
right-most entry is the real one, and anything left of it is whatever the
client sent. That header counts only on a connection from this machine (our
own proxy's): one from anywhere else is its own address, whatever it claims.

"On this machine" also needs the request to be addressed to this machine by
a loopback name (localhost, 127.0.0.1, [::1]; nginx passes the Host on), and
any page it came from (Origin) to be one too. A browser on this machine
opening a page from anywhere else sends that page's own name, whatever the
name points to now.
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


def _name(value: str) -> str:
    """The host in a Host header or an Origin: "localhost:6464" → localhost,
    "[::1]:6464" → ::1, "http://127.0.0.1:6464" → 127.0.0.1."""
    value = value.split("://", 1)[-1].split("/", 1)[0].strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".")


def is_local_name(value: str) -> bool:
    name = _name(value)
    return name == "localhost" or is_loopback(name)


def addressed_here(request: Request) -> bool:
    """Addressed by a loopback name, from no page or a page at one."""
    host = request.headers.get("host")
    if not host or not is_local_name(host):
        return False
    origin = request.headers.get("origin")
    return origin is None or is_local_name(origin)  # "null" (a sandboxed page) isn't one


def on_this_machine(request: Request) -> bool:
    """The request comes from the machine Lumiverb runs on (through nginx or
    not), addressed to it as localhost."""
    return is_loopback(client_address(request)) and addressed_here(request)
