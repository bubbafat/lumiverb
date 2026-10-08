"""The vision AI endpoint: which models it offers (OpenAI-compatible GET /models).

The server asks when someone presses Connect in Settings, so they pick a
model the endpoint actually has; the worker asks before vision work, and
stops (saying why) when the chosen model isn't there.
"""

from __future__ import annotations

import requests

TIMEOUT_SEC = 10.0


class VisionEndpointError(Exception):
    """The endpoint couldn't say which models it has; the message says why, plainly."""


def list_models(api_url: str, api_key: str | None = None, *, timeout: float = TIMEOUT_SEC) -> list[str]:
    """The model ids GET {api_url}/models lists, sorted. Raises VisionEndpointError."""
    url = api_url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise VisionEndpointError("The URL must start with http:// or https://.")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = requests.get(f"{url}/models", headers=headers, timeout=timeout)
    except requests.Timeout:
        raise VisionEndpointError(f"{url} didn't answer within {timeout:.0f} seconds.") from None
    except requests.RequestException as e:
        raise VisionEndpointError(f"Couldn't reach {url}: {type(e).__name__}.") from None
    if resp.status_code in (401, 403):
        raise VisionEndpointError(f"{url} refused the key (HTTP {resp.status_code}).")
    if resp.status_code != 200:
        raise VisionEndpointError(f"{url}/models answered HTTP {resp.status_code}; is the URL right (often it ends in /v1)?")
    try:
        data = resp.json().get("data")
        models = sorted({str(m["id"]) for m in data if isinstance(m, dict) and m.get("id")})
    except (ValueError, AttributeError, TypeError):
        raise VisionEndpointError(f"{url}/models didn't return a model list; is it an OpenAI-compatible endpoint?") from None
    if not models:
        raise VisionEndpointError(f"{url} lists no models.")
    return models


def check_model(api_url: str, api_key: str | None, model: str) -> str | None:
    """None when the endpoint offers the model; otherwise why not, plainly."""
    if not api_url or not model:
        return "No vision model is chosen. An admin picks one in Settings → AI."
    try:
        models = list_models(api_url, api_key)
    except VisionEndpointError as e:
        return str(e)
    if model not in models:
        return f"{api_url} no longer offers {model}. Pick a model in Settings → AI."
    return None
