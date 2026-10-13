"""The HTTP client processing saves through: the Lumiverb API, with a URL
and a key given (the scheduler's per-account key, or the CLI's config).

Errors come back as LumiverbAPIError with the envelope's code and the
status, so whoever called can judge whose they are (src/producers/runner.py).

An idle connection is dropped after KEEPALIVE_SEC, well before the API's
server closes one (uvicorn: 5 s idle). With the two the same, a request sent
on a kept-alive connection just as the server closed it was reset
("Connection reset by peer"), on a busy machine twice in two hours: a vision
save lost, and a scan's preview upload (the clip made, its preview never).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

# Under the API server's keep-alive (uvicorn's timeout_keep_alive, 5 s), by
# more than a busy machine's pause between taking a connection and sending.
KEEPALIVE_SEC = 2.0


class LumiverbAPIError(Exception):
    """A non-2xx response. quiet clients don't print it first."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(f"[{code}]: {message}")


def _error_of(response: httpx.Response) -> LumiverbAPIError:
    try:
        data = response.json()
        error = data.get("error", {})
        message = error.get("message", response.text or str(response.status_code))
        code = error.get("code", "unknown")
    except Exception:  # noqa: BLE001 — not the envelope: say what came back
        message = response.text or f"HTTP {response.status_code}"
        code = "unknown"
    return LumiverbAPIError(code, message, response.status_code)


class ApiClient:
    """One pooled httpx client for the lifetime of the instance; close() when
    done, or use it as a context manager. print_errors: say each error on
    stderr before raising it (the CLI does; the scheduler logs instead)."""

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

    def get(self, path: str, **kwargs: object) -> httpx.Response:
        return self._handle_response(self._client.get(self._url(path), **kwargs))

    @contextmanager
    def stream(self, path: str, **kwargs: object) -> Iterator[httpx.Response]:
        """A streamed GET. A 404 is yielded as it is (commands say "not found"
        their own way); another non-2xx raises."""
        with self._client.stream("GET", self._url(path), **kwargs) as response:
            if response.status_code == 404 or 200 <= response.status_code < 300:
                yield response
                return
            self._raise(response)

    def post(self, path: str, **kwargs: object) -> httpx.Response:
        return self._handle_response(self._client.post(self._url(path), **kwargs))

    def put(self, path: str, **kwargs: object) -> httpx.Response:
        return self._handle_response(self._client.put(self._url(path), **kwargs))

    def patch(self, path: str, **kwargs: object) -> httpx.Response:
        return self._handle_response(self._client.patch(self._url(path), **kwargs))

    def delete(self, path: str, **kwargs: object) -> httpx.Response:
        return self._handle_response(self._client.request("DELETE", self._url(path), **kwargs))

    def raw(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        """The response whatever its status (the caller looks at it: a 204, a 409)."""
        return self._client.request(method, self._url(path), **kwargs)

    def checked(self, response: httpx.Response) -> httpx.Response:
        """A response from raw() whose status wasn't one the caller expected:
        raised as any other call's error (said first, for a client that prints)."""
        return self._handle_response(response)
