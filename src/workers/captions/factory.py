"""Resolve the correct CaptionProvider for a vision_model_id."""

from __future__ import annotations

from src.models.registry import get_model_config
from src.workers.captions.base import CaptionProvider


def get_caption_provider(vision_model_id: str) -> CaptionProvider:
    """
    Return the appropriate CaptionProvider for a vision_model_id.
    Reads LM Studio config from Settings when needed.
    """
    config = get_model_config(vision_model_id)

    if config.caption_provider == "moondream":
        from src.workers.captions.moondream_caption import MoondreamCaptionProvider

        return MoondreamCaptionProvider()

    if config.caption_provider == "qwen_lmstudio":
        from src.core.config import get_settings
        from src.workers.captions.qwen_caption import QwenCaptionProvider

        settings = get_settings()
        return QwenCaptionProvider(
            base_url=settings.lmstudio_url,
            model=settings.lmstudio_vision_model,
        )

    raise ValueError(
        f"No caption provider implementation for {config.caption_provider!r}"
    )

