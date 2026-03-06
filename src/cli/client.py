"""Thin HTTP client for the Lumiverb API; reads CLI config for base URL and auth."""

import sys

import httpx

from src.cli.config import get_api_key, get_api_url


class LumiverbClient:
    """HTTP client that uses CLI config for base_url and Authorization header."""

    def __init__(self) -> None:
        self._base_url = get_api_url().rstrip("/")
        self._api_key = get_api_key()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _handle_response(self, response: httpx.Response) -> httpx.Response:
        if 200 <= response.status_code < 300:
            return response
        try:
            data = response.json()
            error = data.get("error", {})
            message = error.get("message", response.text or str(response.status_code))
            code = error.get("code", "unknown")
            print(f"Error [{code}]: {message}", file=sys.stderr)
        except Exception:
            print(response.text or f"HTTP {response.status_code}", file=sys.stderr)
        sys.exit(1)

    def get(self, path: str, **kwargs: object) -> httpx.Response:
        """GET request; on non-2xx prints error envelope and exits 1."""
        url = f"{self._base_url}{path}" if path.startswith("/") else f"{self._base_url}/{path}"
        with httpx.Client() as client:
            response = client.get(url, headers=self._headers(), timeout=120.0, **kwargs)
            return self._handle_response(response)

    def post(self, path: str, **kwargs: object) -> httpx.Response:
        """POST request; on non-2xx prints error envelope and exits 1."""
        url = f"{self._base_url}{path}" if path.startswith("/") else f"{self._base_url}/{path}"
        with httpx.Client() as client:
            response = client.post(url, headers=self._headers(), timeout=120.0, **kwargs)
            return self._handle_response(response)

    def delete(self, path: str, **kwargs: object) -> httpx.Response:
        """DELETE request; on non-2xx prints error envelope and exits 1."""
        url = f"{self._base_url}{path}" if path.startswith("/") else f"{self._base_url}/{path}"
        with httpx.Client() as client:
            response = client.delete(url, headers=self._headers(), timeout=120.0, **kwargs)
            return self._handle_response(response)
