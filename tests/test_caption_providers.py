"""Fast tests for caption providers."""

import pytest


@pytest.mark.fast
def test_moondream_provider_id():
    from src.workers.captions.moondream_caption import MoondreamCaptionProvider

    assert MoondreamCaptionProvider().provider_id == "moondream"


@pytest.mark.fast
def test_qwen_provider_id():
    from src.workers.captions.qwen_caption import QwenCaptionProvider

    assert QwenCaptionProvider("http://localhost:1234/v1", "qwen").provider_id == "qwen_lmstudio"


@pytest.mark.fast
def test_factory_returns_moondream():
    from src.workers.captions.factory import get_caption_provider
    from src.workers.captions.moondream_caption import MoondreamCaptionProvider

    p = get_caption_provider("moondream")
    assert isinstance(p, MoondreamCaptionProvider)


@pytest.mark.fast
def test_factory_returns_qwen():
    from src.workers.captions.factory import get_caption_provider
    from src.workers.captions.qwen_caption import QwenCaptionProvider

    p = get_caption_provider("qwen")
    assert isinstance(p, QwenCaptionProvider)


@pytest.mark.fast
def test_factory_unknown_model():
    from src.workers.captions.factory import get_caption_provider

    with pytest.raises((KeyError, ValueError)):
        get_caption_provider("unknown_model_xyz")


@pytest.mark.fast
def test_qwen_strips_thinking_blocks():
    from src.workers.captions.qwen_caption import QwenCaptionProvider

    p = QwenCaptionProvider("http://localhost:1234/v1", "qwen")
    raw = "<think>some reasoning here</think>A sunset over mountains."
    assert p._strip_thinking(raw) == "A sunset over mountains."


@pytest.mark.fast
def test_registry_has_model_version():
    from src.models.registry import get_model_config

    assert get_model_config("moondream").model_version == "2"
    assert get_model_config("qwen").model_version == "1"
