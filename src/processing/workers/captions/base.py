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


# The most a machine's longest answer (its max_tokens) may be (Settings › Processing), and the most a reply cut off at
# a smaller one is asked again with.
LONGEST_ANSWER = 4000


class OutOfRoomError(ValueError):
    """A reply cut off at its longest answer with nothing to keep in it. said_nothing: not a word of an answer, all
    of it spent thinking (a model that thinks first, Claude Haiku 5.5 at 500 tokens): every request will end the
    same, so it's the machine's settings at fault, not the image."""

    def __init__(self, tokens: int, *, said_nothing: bool) -> None:
        super().__init__(out_of_room(tokens) + " Raise its Longest answer in Settings › Processing.")
        self.said_nothing = said_nothing


def out_of_room(tokens: int) -> str:
    return (f"It ran out of room: it used all {tokens} tokens of its answer before finishing (a model that thinks "
            "first can spend them all thinking).")


# HTTP answers that are the endpoint's problem, not the image's.
ENDPOINT_FAULT_STATUSES = frozenset({401, 403, 404, 408, 429})


def is_endpoint_fault(error: BaseException) -> bool:
    """Whether a failed call was the endpoint's fault rather than the image's: a redirect too (it's followed
    nowhere: src/shared/ai_requests.py), and an address that isn't allowed (a ConnectionError)."""
    import requests

    from src.shared.ai_requests import Redirected

    if isinstance(error, (requests.ConnectionError, requests.Timeout, Redirected)):
        return True
    if isinstance(error, OutOfRoomError):
        return error.said_nothing
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

