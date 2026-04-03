"""Chain-of-responsibility parsers for extracting retry delay from 429 responses.

Each parser takes a ``requests.Response`` and returns the delay in seconds
as a float, or ``None`` if it cannot extract a value.  The chain is tried
in order; the first non-None result wins.

To add a new provider, write a parser function and append it to ``PARSERS``.
"""

from __future__ import annotations

import re
from typing import Callable

import requests

RetryAfterParser = Callable[[requests.Response], float | None]


# ---------------------------------------------------------------------------
# Standard HTTP
# ---------------------------------------------------------------------------

def parse_retry_after_header(resp: requests.Response) -> float | None:
    """Standard ``Retry-After`` header (seconds).

    Used by OpenAI, Anthropic, Azure OpenAI, and most HTTP-compliant APIs.
    """
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Google Gemini (gRPC-over-HTTP)
# ---------------------------------------------------------------------------

def parse_google_retry_info(resp: requests.Response) -> float | None:
    """Google gRPC RetryInfo in JSON error details.

    Format::

        {"error": {"details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo",
             "retryDelay": "4s"}
        ]}}
    """
    try:
        body = resp.json()
        for detail in body.get("error", {}).get("details", []):
            if detail.get("@type", "").endswith("RetryInfo"):
                raw = detail.get("retryDelay", "")
                if raw and raw.endswith("s"):
                    return float(raw[:-1])
    except Exception:  # noqa: BLE001
        pass
    return None


def parse_google_message_fallback(resp: requests.Response) -> float | None:
    """Fallback: parse "retry in <N>s" from Google error message text.

    Example message fragment::

        Please retry in 4.185577026s.
    """
    try:
        body = resp.json()
        msg = body.get("error", {}).get("message", "")
        m = re.search(r"retry in ([\d.]+)s", msg, re.IGNORECASE)
        if m:
            return float(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------

def parse_default_quota_wait(resp: requests.Response) -> float | None:  # noqa: ARG001
    """Fallback: assume a 60s quota boundary if no provider-specific hint found."""
    return 60.0


PARSERS: list[RetryAfterParser] = [
    parse_retry_after_header,
    parse_google_retry_info,
    parse_google_message_fallback,
    parse_default_quota_wait,
]


def parse_retry_after(resp: requests.Response) -> float:
    """Try each parser in order; return the first non-None result."""
    for parser in PARSERS:
        result = parser(resp)
        if result is not None:
            return result
    return 60.0  # unreachable given default parser, but satisfies type checker
