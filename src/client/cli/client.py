"""The CLI's API client: src/processing/api.py with the URL and key from
~/.lumiverb/config.json (or given), saying each error on stderr."""

from __future__ import annotations

import sys

from src.client.cli.config import get_api_key, get_api_url
from src.processing.api import ApiClient, LumiverbAPIError

__all__ = ["LumiverbAPIError", "LumiverbClient"]


_warned_plain_http = False


def warn_if_plain_http(url: str) -> None:
    """One line on stderr, once per process, when keys would cross the network
    unencrypted: http:// to anything but this machine."""
    global _warned_plain_http
    import ipaddress
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if _warned_plain_http or parts.scheme != "http":
        return
    host = parts.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        _warned_plain_http = True
        print(f"Warning: {host} is plain http; your key is sent unencrypted.", file=sys.stderr)


class LumiverbClient(ApiClient):
    print_errors = True

    def __init__(self, api_key_override: str | None = None, *, base_url: str | None = None,
                 token: str | None = None) -> None:
        key = token or api_key_override
        url = base_url or get_api_url()
        warn_if_plain_http(url)
        super().__init__(url, key if key is not None else get_api_key())
