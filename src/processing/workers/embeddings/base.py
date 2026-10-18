"""Abstract base for embedding providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class EmbeddingError(Exception):
    """A machine couldn't make an image's CLIP vector (ADR-022).

    endpoint_fault: the machine was at fault (unreachable, refused the key,
    the model gone, out of memory), not the image: the work goes to another
    machine, and no clip is charged when none is left. Otherwise the image's
    (one it can't read)."""

    def __init__(self, message: str, *, endpoint_fault: bool) -> None:
        super().__init__(message)
        self.endpoint_fault = endpoint_fault


class EmbeddingProvider(ABC):
    """Produces a fixed-dimension float vector from a proxy image path."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Stable identifier stored in asset_embeddings.model_id."""

    @property
    @abstractmethod
    def model_version(self) -> str:
        """Version string stored in asset_embeddings.model_version."""

    @abstractmethod
    def embed(self, proxy_path: Path) -> list[float]:
        """
        Return embedding vector for the image at proxy_path.
        Raises on failure — caller handles retry/fail logic.
        """

