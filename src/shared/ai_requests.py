"""Every request Lumiverb makes to an AI machine (Settings › AI), Lumiverb's AI
worker among them: to the address the machine is at, and nowhere else.

A machine's URL is checked when it's added, listed or tried
(vision_endpoint.allowed): loopback and the LAN are where Ollama runs, so
they're allowed; a link-local or cloud metadata address is never an AI
machine. Here the same rule holds at each request: the name is looked up
again before it, and the address each connection reached is checked before
anything is sent on it, so a name that resolves elsewhere later can't take
the work, or the machine's key, there. No redirect is followed: a machine
that answers with one is at fault (Redirected), as one that's unreachable
is, and where it pointed is the log's.

What a machine answers when it fails is the log's too (Admin › Logs, the
server admin's): what it says goes to every signed-in person (a machine's
status, why work waits, a clip's failure) only in Lumiverb's words
(`answered`).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

logger = logging.getLogger(__name__)

# Link-local and cloud metadata addresses are never an AI machine. Loopback
# and private (LAN) addresses are allowed: ollama often runs there.
BLOCKED = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)

# How much of what a machine answered goes to the log, in a line.
LOGGED_UP_TO = 1000


def refused(address: str) -> bool:
    """Whether an address (an IP; an IPv6 one with or without its %zone) is one no AI machine is at."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in BLOCKED)


def name_refused(host: str, port: int) -> bool:
    """Whether host resolves to an address refused() refuses. OSError when it doesn't resolve."""
    return any(refused(str(sockaddr[0]))
               for *_, sockaddr in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP))


class AddressRefused(requests.ConnectionError):
    """Nothing was sent: the machine's name resolves to, or its connection reached, an address no AI machine is
    at. The machine's fault, as one that's unreachable."""


class Redirected(requests.RequestException):
    """The machine answered with a redirect, which Lumiverb doesn't follow: its fault. response: its answer."""


class _RefusedError(Exception):
    """A connection reached a refused address. Not an OSError, so urllib3 and requests pass it on as it is (closing
    the connection) for request() to say."""


def _checked(sock: socket.socket) -> socket.socket:
    peer = str(sock.getpeername()[0])
    if refused(peer):
        sock.close()
        raise _RefusedError(peer)
    return sock


class _Connection(HTTPConnection):
    def _new_conn(self) -> socket.socket:
        return _checked(super()._new_conn())


class _TLSConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:  # before its TLS handshake
        return _checked(super()._new_conn())


class _Pool(HTTPConnectionPool):
    ConnectionCls = _Connection


class _TLSPool(HTTPSConnectionPool):
    ConnectionCls = _TLSConnection


_POOLS = {"http": _Pool, "https": _TLSPool}


class _Adapter(HTTPAdapter):
    """requests' own, each connection's address checked as it's made (through a proxy, the proxy's: the machine's
    own name is checked by request() before)."""

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = _POOLS

    def proxy_manager_for(self, proxy: str, **kwargs: Any) -> Any:
        manager = super().proxy_manager_for(proxy, **kwargs)
        if not proxy.lower().startswith("socks"):  # a SOCKS proxy's connections are its own kind
            manager.pool_classes_by_scheme = _POOLS
        return manager


def request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """requests.request to an AI machine, never followed elsewhere. Raises AddressRefused (nothing sent),
    Redirected, and whatever else requests raises."""
    parts = urlsplit(url)
    try:
        name_is_refused = name_refused(parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80))
    except (OSError, UnicodeError, ValueError):
        name_is_refused = False  # one that doesn't resolve: requests says so
    if name_is_refused:
        logger.warning("AI machine %s: its name resolves to an address that isn't allowed; nothing was sent", url)
        raise AddressRefused(f"{url} is at an address that isn't allowed.")
    kwargs["allow_redirects"] = False
    with requests.Session() as session:
        adapter = _Adapter()
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        try:
            resp = session.request(method, url, **kwargs)
        except _RefusedError as e:
            logger.warning("AI machine %s: connected to %s, which isn't allowed; nothing was sent", url, e)
            raise AddressRefused(f"{url} is at an address that isn't allowed.") from None
    if 300 <= resp.status_code < 400:
        logger.warning("AI machine %s answered HTTP %s, a redirect to %s, which Lumiverb doesn't follow",
                       url, resp.status_code, resp.headers.get("Location") or "nowhere")
        raise Redirected(f"{url} answered with a redirect, which Lumiverb doesn't follow.", response=resp)
    return resp


def get(url: str, **kwargs: Any) -> requests.Response:
    return request("GET", url, **kwargs)


def post(url: str, **kwargs: Any) -> requests.Response:
    return request("POST", url, **kwargs)


def answered(resp: requests.Response, *, model: str = "", api: str = "") -> str:
    """What an answer that isn't the work says, in Lumiverb's words: "refused the key", "has no <model> or no
    <api> API there", "answered 500". What the machine said with it is logged, never in these words."""
    status = resp.status_code
    said = " ".join(str(getattr(resp, "text", "") or "").split())
    logger.warning("AI machine %s answered HTTP %s: %s", getattr(resp, "url", "") or "?", status,
                   said[:LOGGED_UP_TO] or "(nothing)")
    if status in (401, 403):
        return "refused the key"
    if status == 404 and (model or api):
        return f"has no {model or 'such model'}" + (f" or no {api} API there" if api else "")
    return f"answered {status}"
