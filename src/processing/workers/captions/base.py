"""Abstract base for caption providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class CaptionError(Exception):
    """The vision model couldn't describe or read an image, after retries.

    endpoint_fault: the endpoint was at fault (unreachable, timed out,
    refused the key, the model gone, overloaded, a server error), not the
    image; the worker then stops vision work instead of charging the clip.
    """

    def __init__(self, message: str, *, endpoint_fault: bool) -> None:
        super().__init__(message)
        self.endpoint_fault = endpoint_fault


# HTTP answers that are the endpoint's problem, not the image's.
ENDPOINT_FAULT_STATUSES = frozenset({401, 403, 404, 408, 429})


def is_endpoint_fault(error: BaseException) -> bool:
    """Whether a failed call was the endpoint's fault rather than the image's."""
    import requests

    if isinstance(error, (requests.ConnectionError, requests.Timeout)):
        return True
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return isinstance(status, int) and (status in ENDPOINT_FAULT_STATUSES or status >= 500)


class CaptionProvider(ABC):
    """Generates a description and tags from a proxy image path."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Provider identifier for logging/provenance."""

    @abstractmethod
    def describe(self, proxy_path: Path) -> dict:
        """
        Return dict with keys:
            description: str
            tags: list[str]
        Raises CaptionError when it can't (saying whether the endpoint was at fault).
        """

