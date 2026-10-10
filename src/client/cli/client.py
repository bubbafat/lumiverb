"""The CLI's API client: src/processing/api.py with the URL and key from
~/.lumiverb/config.json (or given), saying each error on stderr."""

from __future__ import annotations

from src.client.cli.config import get_api_key, get_api_url
from src.processing.api import ApiClient, LumiverbAPIError

__all__ = ["LumiverbAPIError", "LumiverbClient"]


class LumiverbClient(ApiClient):
    print_errors = True

    def __init__(self, api_key_override: str | None = None, *, base_url: str | None = None,
                 token: str | None = None) -> None:
        key = token or api_key_override
        super().__init__(base_url or get_api_url(), key if key is not None else get_api_key())
