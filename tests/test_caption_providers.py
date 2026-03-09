"""Fast tests for caption providers."""

import pytest


@pytest.mark.fast
def test_moondream_provider_id():
    from src.workers.captions.moondream_caption import MoondreamCaptionProvider

    assert MoondreamCaptionProvider().provider_id == "moondream"


@pytest.mark.fast
def test_openai_provider_id():
    from src.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    assert (
        OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "qwen").provider_id
        == "openai_compatible"
    )


@pytest.mark.fast
def test_factory_returns_moondream():
    from src.workers.captions.factory import get_caption_provider
    from src.workers.captions.moondream_caption import MoondreamCaptionProvider

    p = get_caption_provider("moondream")
    assert isinstance(p, MoondreamCaptionProvider)


@pytest.mark.fast
def test_factory_returns_openai_compatible():
    from src.workers.captions.factory import get_caption_provider
    from src.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = get_caption_provider("qwen3-visioncaption-2b")
    assert isinstance(p, OpenAICompatibleCaptionProvider)
    assert p._model == "qwen3-visioncaption-2b"


@pytest.mark.fast
def test_factory_arbitrary_model_id():
    from src.workers.captions.factory import get_caption_provider
    from src.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = get_caption_provider("llava:13b")
    assert isinstance(p, OpenAICompatibleCaptionProvider)
    assert p._model == "llava:13b"


@pytest.mark.fast
def test_openai_strips_thinking_blocks():
    from src.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "qwen")
    raw = "<think>some reasoning here</think>A sunset over mountains."
    assert p._strip_thinking(raw) == "A sunset over mountains."


@pytest.mark.fast
def test_embedding_config_moondream():
    from src.models.registry import get_embedding_config

    config = get_embedding_config("moondream")
    assert config.embedding_provider == "moondream"
    assert config.embedding_dim == 512
    assert config.moondream_weight == 0.3
    assert config.clip_weight == 0.7


@pytest.mark.fast
def test_embedding_config_default_fallback():
    from src.models.registry import get_embedding_config

    config = get_embedding_config("qwen3-visioncaption-2b")
    assert config.embedding_provider == "clip"
    assert config.embedding_dim == 512
    assert config.moondream_weight == 0.0
    assert config.clip_weight == 1.0


@pytest.mark.fast
def test_model_version_for_provenance():
    from src.models.registry import model_version_for_provenance

    assert model_version_for_provenance("moondream") == "2"
    assert model_version_for_provenance("qwen3-visioncaption-2b") == "1"
    assert model_version_for_provenance("llava:13b") == "1"
