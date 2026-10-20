"""Simple in-memory sliding-window rate limiter.

Not distributed — suitable for single-process deployments. Automatically
evicts expired entries to prevent unbounded memory growth.
"""

from __future__ import annotations

import threading
import time

from fastapi import HTTPException, Request

from src.server.api.client_address import client_address


class RateLimiter:
    """Sliding window counter keyed by client IP."""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _client_ip(self, request: Request) -> str:
        return client_address(request)  # nginx's view of it (client_address.py)

    def check(self, request: Request, key: str | None = None) -> None:
        """Raise 429 if the caller (or ``key``, e.g. an email) has exceeded the rate limit."""
        ip = key if key is not None else self._client_ip(request)
        now = time.monotonic()
        cutoff = now - self.window

        with self._lock:
            hits = self._hits.get(ip, [])
            # Prune expired entries
            hits = [t for t in hits if t > cutoff]

            if len(hits) >= self.max_requests:
                self._hits[ip] = hits
                raise HTTPException(
                    status_code=429,
                    detail="Too many requests. Please try again later.",
                )
            hits.append(now)
            self._hits[ip] = hits

            # Periodic eviction: if dict grows large, prune stale IPs
            if len(self._hits) > 10_000:
                stale = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
                for k in stale:
                    del self._hits[k]


# Shared limiters for auth endpoints
login_limiter = RateLimiter(max_requests=5, window_seconds=60)
forgot_password_limiter = RateLimiter(max_requests=3, window_seconds=60)
reset_password_limiter = RateLimiter(max_requests=5, window_seconds=60)
# Searches by someone not signed in (a public library's or project's page): a person browsing asks a few a
# minute (a page of results, its facets, more like this); a script asking without end is held to two a second.
public_search_limiter = RateLimiter(max_requests=120, window_seconds=60)
# Search by a picture (signed in): CLIP and a face model look at each one, seconds of CPU or GPU apiece.
image_search_limiter = RateLimiter(max_requests=20, window_seconds=60)


def image_search_limited(request: Request) -> None:
    """A route's dependency: a search by picture counts against image_search_limiter, by address; 429 past it."""
    image_search_limiter.check(request)


def anonymous_search_limited(request: Request) -> None:
    """A route's dependency: a search from a public page (request.state.is_public_request, set by the
    middleware for a caller who isn't signed in) counts against public_search_limiter, by address; 429 past it.
    Signed in, nothing is counted."""
    if getattr(request.state, "is_public_request", False):
        public_search_limiter.check(request)
