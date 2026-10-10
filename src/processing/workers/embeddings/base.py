"""Abstract base for embedding providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


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

