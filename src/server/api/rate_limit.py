"""Simple in-memory sliding-window rate limiter.

Not distributed — suitable for single-process deployments. Automatically
evicts expired entries to prevent unbounded memory growth.
"""

from __future__ import annotations

import threading
import time

from fastapi import HTTPException, Request


class RateLimiter:
    """Sliding window counter keyed by client IP."""

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def check(self, request: Request) -> None:
        """Raise 429 if the caller has exceeded the rate limit."""
        ip = self._client_ip(request)
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
