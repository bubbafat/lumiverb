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
        self._take(key if key is not None else self._client_ip(request), count=True)

    def held(self, key: str) -> None:
        """Raise 429 if ``key`` has reached the limit, counting nothing: what's
        counted is counted with ``count`` (a failed sign-in, say)."""
        self._take(key, count=False)

    def count(self, key: str) -> None:
        """One more for ``key``, whatever the limit."""
        now = time.monotonic()
        with self._lock:
            self._hits[key] = [*self._recent(key, now), now]

    def forget(self, key: str) -> None:
        """Nothing counted for ``key`` any more."""
        with self._lock:
            self._hits.pop(key, None)

    def _recent(self, key: str, now: float) -> list[float]:
        return [t for t in self._hits.get(key, []) if t > now - self.window]

    def _take(self, key: str, *, count: bool) -> None:
        now = time.monotonic()
        cutoff = now - self.window

        with self._lock:
            hits = self._recent(key, now)
            if len(hits) >= self.max_requests:
                self._hits[key] = hits
                raise HTTPException(
                    status_code=429,
                    detail="Too many requests. Please try again later.",
                )
            if count:
                hits.append(now)
            self._hits[key] = hits

            # Periodic eviction: if dict grows large, prune stale keys
            if len(self._hits) > 10_000:
                stale = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
                for k in stale:
                    del self._hits[k]


def _here(request: Request, name: str) -> str:
    return f"{name} from {client_address(request)}"


def name_held(request: Request, name: str, here: RateLimiter, anywhere: RateLimiter) -> None:
    """429 when `name` (an email, a person) has reached its limit from this
    address (`here`), or from anywhere (`anywhere`); counts nothing."""
    here.held(_here(request, name))
    anywhere.held(name)


def name_counted(request: Request, name: str, here: RateLimiter, anywhere: RateLimiter) -> None:
    """One more for `name`, from this address and from anywhere."""
    here.count(_here(request, name))
    anywhere.count(name)


def name_cleared(request: Request, name: str, here: RateLimiter) -> None:
    """What this address counted for `name` forgotten (the right password, at last)."""
    here.forget(_here(request, name))


# Shared limiters for auth endpoints.
#
# Signing in is counted by address, every try (a script trying many emails from one place), and by email, only the
# failed ones: a password guessed at is held back, and nobody keeps someone else out by sending their email, as
# their own right password never counts. A wrong one counts against the email from this address and from anywhere;
# the first is held five times sooner, so one address guessing at someone's password is stopped long before the
# email is held back from everyone (it takes five addresses failing as fast as they're let), and its owner signs in
# from their own address meanwhile. A sign-in forgets its address's failures.
login_limiter = RateLimiter(max_requests=5, window_seconds=60)
login_failures_here = RateLimiter(max_requests=5, window_seconds=15 * 60)
login_failures = RateLimiter(max_requests=25, window_seconds=15 * 60)
# A reset link asked for: by address, every request; by email, each link made (and mailed), from this address and
# from anywhere, the first tighter as for signing in, so one address asking for someone's link again and again is
# stopped before the owner's own request is held back, and their mailbox gets ten at most in 15 minutes.
forgot_password_limiter = RateLimiter(max_requests=3, window_seconds=60)
forgot_password_here = RateLimiter(max_requests=3, window_seconds=15 * 60)
forgot_password_anywhere = RateLimiter(max_requests=10, window_seconds=15 * 60)
reset_password_limiter = RateLimiter(max_requests=5, window_seconds=60)
# Changing a password checks the current one: a session left open on someone else's screen can't be used to
# guess it. By address, every try; by the person, only wrong current passwords, from this address and from
# anywhere, as for signing in: someone using a session of theirs elsewhere can't keep them from changing it.
change_password_limiter = RateLimiter(max_requests=5, window_seconds=60)
change_password_failures_here = RateLimiter(max_requests=5, window_seconds=15 * 60)
change_password_failures = RateLimiter(max_requests=25, window_seconds=15 * 60)
# What a browser couldn't load (routers/me.py report_failed_requests): it sends once a request gets through, so a
# person sends a few a minute at most; a script filling the log is held back (by address, and by the person).
failed_requests_limiter = RateLimiter(max_requests=10, window_seconds=60)
# Try it as a viewer (src/server/demo.py): a visitor signs in once a visit; a script making sessions is held back.
demo_limiter = RateLimiter(max_requests=10, window_seconds=60)
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
