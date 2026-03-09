"""Moondream caption provider. Wraps src/workers/vision.py."""

from __future__ import annotations

from pathlib import Path

from src.workers.captions.base import CaptionProvider


class MoondreamCaptionProvider(CaptionProvider):

    @property
    def provider_id(self) -> str:
        return "moondream"

    def describe(self, proxy_path: Path) -> dict:
        from src.workers.vision import describe_image

        return describe_image(proxy_path)

