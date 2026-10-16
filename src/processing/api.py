"""The HTTP client processing saves through: the Lumiverb API, with a URL
and a key given (the scheduler's per-account key, or the CLI's config).

Errors come back as LumiverbAPIError with the envelope's code and the
status, so whoever called can judge whose they are (src/producers/runner.py).

An idle connection is dropped after KEEPALIVE_SEC, well before the API's
server closes one (uvicorn: 5 s idle). With the two the same, a request sent
on a kept-alive connection just as the server closed it was reset
("Connection reset by peer"), on a busy machine twice in two hours: a vision
save lost, and a scan's preview upload (the clip made, its preview never).

A call is tried again when it may not have reached the API, or its answer
was lost on the way back (a connection refused, reset or dropped, the
server gone mid-answer, no answer in time), or the API is restarting
behind nginx (502, 503, 504): after RETRY_DELAYS, while within
RETRY_WITHIN_SEC of the first failure, about the time an API takes to
restart. Never after any other answer: a 4xx or a 500 is the API's word.
A GET is always tried again. Another method only when its caller says the
call is safe to send twice (retry=True): the API does it once whatever
number of times it arrives, or answers a repeat as it answered the first
(the scan's calls, routers/scans.py). Anything else, an upload say, would
be done twice, so its caller judges a failure as it always has.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager

import httpx

logger = logging.getLogger(__name__)

# Under the API server's keep-alive (uvicorn's timeout_keep_alive, 5 s), by
# more than a busy machine's pause between taking a connection and sending.
KEEPALIVE_SEC = 2.0

# The waits before each try again: 31.5 s in all.
RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
# No try again starts later than this after the first failure: a call that
# fails slowly (no answer within the timeout) is tried once more, not six times.
RETRY_WITHIN_SEC = 35.0
# nginx's word for an API that isn't answering (restarting), or too slowly.
RETRY_STATUSES = frozenset({502, 503, 504})
# The call may not have arrived, or its answer was lost.
TRANSIENT_ERRORS = (httpx.NetworkError, httpx.RemoteProtocolError, httpx.TimeoutException)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


class LumiverbAPIError(Exception):
    """A non-2xx response. quiet clients don't print it first."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(f"[{code}]: {message}")


# A body this long, or with markup in it, isn't a message: a proxy's or another server's page.
_SAID_UP_TO = 300


def _not_the_apis(response: httpx.Response) -> str:
    """An answer that isn't the API's error envelope: its status and where
    from, and its body only when that's a short line of text (never a proxy's
    HTML page). 502-504 are a proxy saying the API behind it isn't answering."""
    status = f"HTTP {response.status_code} {response.reason_phrase}".strip()
    origin = f"{response.request.url.scheme}://{response.request.url.netloc.decode()}"
    if response.status_code in RETRY_STATUSES:
        return f"{status} from {origin}: Lumiverb isn't answering behind it (starting up?). Try again in a moment."
    text = response.text.strip()
    if text and len(text) <= _SAID_UP_TO and "<" not in text and "\n" not in text:
        return f"{status} from {origin}: {text}"
    return f"{status} from {origin}, not an answer from Lumiverb's API: is that where it is?"


def _error_of(response: httpx.Response) -> LumiverbAPIError:
    try:
        data = response.json()
        error = data["error"]
        return LumiverbAPIError(error.get("code", "unknown"), error.get("message") or _not_the_apis(response),
                                response.status_code)
    except Exception:  # noqa: BLE001 — not the envelope: say what came back
        return LumiverbAPIError("unknown", _not_the_apis(response), response.status_code)


class ApiClient:
    """One pooled httpx client for the lifetime of the instance; close() when
    done, or use it as a context manager. print_errors: say each error on
    stderr before raising it, and each try again (the CLI does; the
    scheduler logs instead)."""

    print_errors = False

    def __init__(self, base_url: str, token: str | None, *, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(headers=headers, timeout=timeout,
                                    limits=httpx.Limits(keepalive_expiry=KEEPALIVE_SEC))

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def token(self) -> str | None:
        return self._api_key

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}" if path.startswith("/") else f"{self._base_url}/{path}"

    def _raise(self, response: httpx.Response) -> None:
        error = _error_of(response)
        if self.print_errors:
            print(f"Error [{error.code}]: {error.message}", file=sys.stderr)
        raise error

    def _handle_response(self, response: httpx.Response) -> httpx.Response:
        if 200 <= response.status_code < 300:
            return response
        self._raise(response)
        raise AssertionError("unreachable")

    def _again(self, method: str, path: str, why: str, delay: float) -> None:
        """Said once for each try again: the log's INFO, and the CLI's stderr."""
        logger.info("%s %s: %s; trying again in %g s", method, path, why, delay)
        if self.print_errors:
            print(f"Lumiverb didn't answer ({why}): trying again in {delay:g} s", file=sys.stderr)

    def _tries(self, method: str, path: str, retry: bool) -> Iterator[Callable[[str], bool]]:
        """One step for each try. Each step's call, given why its try failed,
        says whether to try again (having waited) or give up."""
        delays = iter(RETRY_DELAYS if retry else ())
        first_failure: float | None = None

        def again(why: str) -> bool:
            nonlocal first_failure
            delay = next(delays, None)
            now = time.monotonic()
            first_failure = now if first_failure is None else first_failure
            if delay is None or now - first_failure + delay > RETRY_WITHIN_SEC:
                return False
            self._again(method, path, why, delay)
            _sleep(delay)
            return True

        while True:
            yield again

    def _send(self, method: str, path: str, *, retry: bool, **kwargs: object) -> httpx.Response:
        """The response, whatever its status, once a try gets one that isn't
        transient (or the tries run out: then the last one's error or answer)."""
        for again in self._tries(method, path, retry):
            try:
                response = self._client.request(method, self._url(path), **kwargs)
            except TRANSIENT_ERRORS as exc:
                if again(type(exc).__name__ + (f": {exc}" if str(exc) else "")):
                    continue
                raise
            if response.status_code in RETRY_STATUSES and again(f"HTTP {response.status_code}"):
                response.close()
                continue
            return response
        raise AssertionError("unreachable")

    def get(self, path: str, **kwargs: object) -> httpx.Response:
        return self._handle_response(self._send("GET", path, retry=True, **kwargs))

    @contextmanager
    def stream(self, path: str, **kwargs: object) -> Iterator[httpx.Response]:
        """A streamed GET, tried again as any GET until it starts. A 404 is
        yielded as it is (commands say "not found" their own way); another
        non-2xx raises."""
        for again in self._tries("GET", path, True):
            with ExitStack() as opened:
                try:
                    response = opened.enter_context(self._client.stream("GET", self._url(path), **kwargs))
                except TRANSIENT_ERRORS as exc:
                    if again(type(exc).__name__ + (f": {exc}" if str(exc) else "")):
                        continue
                    raise
                if response.status_code in RETRY_STATUSES and again(f"HTTP {response.status_code}"):
                    continue
                if response.status_code == 404 or 200 <= response.status_code < 300:
                    yield response
                    return
                response.read()
                self._raise(response)

    def post(self, path: str, *, retry: bool = False, **kwargs: object) -> httpx.Response:
        """retry: the call is safe to send twice (see the module's docstring)."""
        return self._handle_response(self._send("POST", path, retry=retry, **kwargs))

    def put(self, path: str, *, retry: bool = False, **kwargs: object) -> httpx.Response:
        return self._handle_response(self._send("PUT", path, retry=retry, **kwargs))

    def patch(self, path: str, *, retry: bool = False, **kwargs: object) -> httpx.Response:
        return self._handle_response(self._send("PATCH", path, retry=retry, **kwargs))

    def delete(self, path: str, *, retry: bool = False, **kwargs: object) -> httpx.Response:
        return self._handle_response(self._send("DELETE", path, retry=retry, **kwargs))

    def raw(self, method: str, path: str, *, retry: bool | None = None, **kwargs: object) -> httpx.Response:
        """The response whatever its status (the caller looks at it: a 204, a
        409). retry: as for post; a GET's always is."""
        return self._send(method, path, retry=method.upper() == "GET" if retry is None else retry, **kwargs)

    def checked(self, response: httpx.Response) -> httpx.Response:
        """A response from raw() whose status wasn't one the caller expected:
        raised as any other call's error (said first, for a client that prints)."""
        return self._handle_response(response)
