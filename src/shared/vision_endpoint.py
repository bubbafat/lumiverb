"""The vision AI endpoint: which models it offers (OpenAI-compatible GET /models).

The server asks when someone presses Connect in Settings, so they pick a
model the endpoint actually has; the worker asks before vision work, and
stops (saying why) when the chosen model isn't there.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

TIMEOUT_SEC = 10.0

# Said for every way the endpoint can fail, so the answer can't map a
# network; the detail goes to the log.
UNREACHABLE = "Couldn't reach the AI machine."

# Link-local and cloud metadata addresses are never an AI machine. Loopback
# and private (LAN) addresses are allowed: ollama often runs there.
_BLOCKED = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
)


class VisionEndpointError(Exception):
    """The endpoint couldn't say which models it has; the message says why, plainly."""


def check_url(api_url: str) -> str:
    """The URL, trimmed, when it's a plain http(s) URL: no query, fragment
    or user info. Raises VisionEndpointError."""
    url = api_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise VisionEndpointError("The URL must start with http:// or https://.")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError:
        raise VisionEndpointError("That isn't a valid URL.") from None
    if not parts.hostname:
        raise VisionEndpointError("That isn't a valid URL.")
    if "?" in url or "#" in url or "@" in parts.netloc:
        raise VisionEndpointError("The URL can't have a query, fragment or user info.")
    return url


def _blocked(host: str, port: int) -> bool:
    """Whether host resolves to a link-local or metadata address. OSError when it doesn't resolve."""
    for *_, sockaddr in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
        ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if any(ip in net for net in _BLOCKED):
            return True
    return False


def _unreachable(url: str, why: str) -> VisionEndpointError:
    logger.warning("AI machine %s: %s", url, why)
    return VisionEndpointError(UNREACHABLE)


def list_models(api_url: str, api_key: str | None = None, *, timeout: float = TIMEOUT_SEC) -> list[str]:
    """The model ids GET {api_url}/models lists, sorted. Raises VisionEndpointError."""
    url = check_url(api_url)
    parts = urlsplit(url)
    try:
        blocked = _blocked(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except (OSError, UnicodeError) as e:
        raise _unreachable(url, f"doesn't resolve ({e})") from None
    if blocked:
        raise VisionEndpointError("That address isn't allowed.")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        # No redirects: the answer is the URL's own (and the key goes nowhere else).
        resp = requests.get(f"{url}/models", headers=headers, timeout=timeout, allow_redirects=False)
    except requests.Timeout:
        raise _unreachable(url, f"no answer within {timeout:.0f} seconds") from None
    except requests.RequestException as e:
        raise _unreachable(url, type(e).__name__) from None
    if resp.status_code != 200:
        raise _unreachable(url, f"/models answered HTTP {resp.status_code}")
    try:
        data = resp.json().get("data")
        models = sorted({str(m["id"]) for m in data if isinstance(m, dict) and m.get("id")})
    except (ValueError, AttributeError, TypeError):
        raise _unreachable(url, "/models didn't return a model list") from None
    if not models:
        raise _unreachable(url, "lists no models")
    return models


def check_model(api_url: str, api_key: str | None, model: str) -> str | None:
    """None when the endpoint offers the model; otherwise why not, plainly."""
    if not api_url or not model:
        return "No vision model is chosen. An admin picks one in Settings → AI."
    try:
        models = list_models(api_url, api_key)
    except VisionEndpointError as e:
        return str(e)
    if model not in models:
        return f"{api_url} no longer offers {model}. Pick a model in Settings → AI."
    return None
