"""Auto-discover vision model ID from an OpenAI-compatible /models endpoint."""

from __future__ import annotations

import logging
import threading

import requests

logger = logging.getLogger(__name__)

_cache: dict[str, str] = {}
_lock = threading.Lock()


def discover_model_id(api_url: str, api_key: str | None = None) -> str:
    """Call GET {api_url}/models and return the first model ID.

    Result is cached per api_url so repeated calls (e.g. per-image during
    ingest) only hit the network once.
    """
    with _lock:
        if api_url in _cache:
            return _cache[api_url]

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resp = requests.get(f"{api_url.rstrip('/')}/models", headers=headers, timeout=10)
    resp.raise_for_status()
    models = resp.json().get("data", [])
    if not models:
        raise RuntimeError(f"No models available at {api_url}/models")

    model_id: str = models[0]["id"]
    with _lock:
        _cache[api_url] = model_id
    logger.info("Auto-discovered vision model: %s from %s", model_id, api_url)
    return model_id


def resolve_vision_model_id(
    *,
    client_model_id: str = "",
    tenant_model_id: str = "",
    api_url: str = "",
    api_key: str | None = None,
) -> str:
    """Resolve vision model ID: client config > tenant config > auto-discover."""
    if client_model_id:
        return client_model_id
    if tenant_model_id:
        return tenant_model_id
    if api_url:
        return discover_model_id(api_url, api_key)
    raise ValueError(
        "Cannot resolve vision model: no model ID configured and no API URL for discovery"
    )


def clear_cache() -> None:
    """Clear cached model IDs. For testing."""
    with _lock:
        _cache.clear()
