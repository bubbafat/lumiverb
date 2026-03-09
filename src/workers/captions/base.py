"""Abstract base for caption providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class CaptionProvider(ABC):
    """Generates a description and tags from a proxy image path."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Matches ModelConfig.caption_provider in the registry."""

    @abstractmethod
    def describe(self, proxy_path: Path) -> dict:
        """
        Return dict with keys:
            description: str
            tags: list[str]
        Returns empty dict on failure — caller handles fail_job.
        """

