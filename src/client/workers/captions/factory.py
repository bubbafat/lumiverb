"""Caption provider factory."""

from __future__ import annotations

from src.client.workers.captions.base import CaptionProvider
from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider


def get_caption_provider(
    vision_model_id: str,
    api_url: str,
    api_key: str | None = None,
) -> CaptionProvider:
    """Return an OpenAI-compatible CaptionProvider for the given model."""
    return OpenAICompatibleCaptionProvider(
        base_url=api_url,
        model=vision_model_id,
        api_key=api_key or None,
    )
