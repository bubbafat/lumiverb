"""Thin HTTP client for the Lumiverb API; reads CLI config for base URL and auth."""

import sys
from contextlib import contextmanager
from typing import Iterator

import httpx

from src.cli.config import get_api_key, get_api_url


class LumiverbAPIError(Exception):
    """Raised when the API returns a non-2xx response. The error message is printed to stderr before raising."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(f"[{code}]: {message}")


def _print_and_raise(response: httpx.Response) -> None:
    """Parse the error envelope, print to stderr, then raise LumiverbAPIError."""
    try:
        data = response.json()
        error = data.get("error", {})
        message = error.get("message", response.text or str(response.status_code))
        code = error.get("code", "unknown")
    except Exception:
        message = response.text or f"HTTP {response.status_code}"
        code = "unknown"
    print(f"Error [{code}]: {message}", file=sys.stderr)
    raise LumiverbAPIError(code, message, response.status_code)


class LumiverbClient:
    """HTTP client that uses CLI config for base_url and Authorization header.

    Reuses a single httpx.Client with connection pooling for the lifetime of the
    instance.  Call .close() when done, or use as a context manager.
    """

    def __init__(self, api_key_override: str | None = None) -> None:
        self._base_url = get_api_url().rstrip("/")
        self._api_key = api_key_override if api_key_override is not None else get_api_key()
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._client = httpx.Client(headers=headers, timeout=120.0)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LumiverbClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}" if path.startswith("/") else f"{self._base_url}/{path}"

    def _handle_response(self, response: httpx.Response) -> httpx.Response:
        if 200 <= response.status_code < 300:
            return response
        _print_and_raise(response)

    def get(self, path: str, **kwargs: object) -> httpx.Response:
        """GET request; on non-2xx prints error envelope and raises LumiverbAPIError."""
        response = self._client.get(self._url(path), **kwargs)
        return self._handle_response(response)

    @contextmanager
    def stream(self, path: str, **kwargs: object) -> Iterator[httpx.Response]:
        """
        Stream a GET request. Yields the httpx.Response for iteration via iter_bytes().

        For 2xx responses, the caller can consume the body as a stream.
        A 404 response is yielded to the caller without exiting, so commands can
        handle "not found" gracefully. Other non-2xx responses are printed and
        raise LumiverbAPIError.
        """
        with self._client.stream("GET", self._url(path), **kwargs) as response:
            if response.status_code == 404:
                yield response
                return
            if 200 <= response.status_code < 300:
                yield response
                return
            _print_and_raise(response)

    def post(self, path: str, **kwargs: object) -> httpx.Response:
        """POST request; on non-2xx prints error envelope and raises LumiverbAPIError."""
        response = self._client.post(self._url(path), **kwargs)
        return self._handle_response(response)

    def patch(self, path: str, **kwargs: object) -> httpx.Response:
        """PATCH request; on non-2xx prints error envelope and raises LumiverbAPIError."""
        response = self._client.patch(self._url(path), **kwargs)
        return self._handle_response(response)

    def delete(self, path: str, **kwargs: object) -> httpx.Response:
        """DELETE request; on non-2xx prints error envelope and raises LumiverbAPIError."""
        response = self._client.request("DELETE", self._url(path))
        return self._handle_response(response)

    def raw(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        """
        Raw request that always returns the response without error handling or sys.exit.
        Use this when the caller needs to inspect non-2xx status codes (e.g. 204, 409).
        """
        return self._client.request(method, self._url(path), **kwargs)

