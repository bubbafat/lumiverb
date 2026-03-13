"""Caption provider factory. Convention-based routing."""

from __future__ import annotations

from src.workers.captions.base import CaptionProvider


def get_caption_provider(vision_model_id: str) -> CaptionProvider:
    """
    Return the appropriate CaptionProvider for a vision_model_id.

    Convention:
        "moondream"    → MoondreamCaptionProvider (local SDK)
        anything else  → OpenAICompatibleCaptionProvider
                         (model=vision_model_id, url from settings)
    """
    if vision_model_id == "moondream":
        from src.workers.captions.moondream_caption import MoondreamCaptionProvider

        return MoondreamCaptionProvider()

    from src.core.config import get_settings
    from src.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    settings = get_settings()
    return OpenAICompatibleCaptionProvider(
        base_url=settings.vision_api_url,
        model=vision_model_id,
        api_key=settings.vision_api_key or None,
    )
