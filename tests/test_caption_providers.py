"""Fast tests for caption providers."""

import pytest


@pytest.mark.fast
def test_openai_provider_id():
    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    assert (
        OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "qwen").provider_id
        == "openai_compatible"
    )


@pytest.mark.fast
def test_factory_returns_openai_compatible():
    from src.client.workers.captions.factory import get_caption_provider
    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = get_caption_provider(
        vision_model_id="qwen3-visioncaption-2b",
        api_url="http://localhost:1234/v1",
        api_key=None,
    )
    assert isinstance(p, OpenAICompatibleCaptionProvider)
    assert p._model == "qwen3-visioncaption-2b"


@pytest.mark.fast
def test_factory_arbitrary_model_id():
    from src.client.workers.captions.factory import get_caption_provider
    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = get_caption_provider(
        vision_model_id="llava:13b",
        api_url="http://localhost:1234/v1",
        api_key=None,
    )
    assert isinstance(p, OpenAICompatibleCaptionProvider)
    assert p._model == "llava:13b"


@pytest.mark.fast
def test_openai_strips_thinking_blocks():
    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "qwen")
    raw = "<think>some reasoning here</think>A sunset over mountains."
    assert p._strip_thinking(raw) == "A sunset over mountains."


@pytest.mark.fast
def test_openai_provider_sends_auth_header_when_api_key_set() -> None:
    """When api_key is provided, _chat sends Authorization: Bearer <key>."""
    from unittest.mock import MagicMock, patch

    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "gpt-4o", api_key="sk-test")
    assert p._api_key == "sk-test"

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": '{"description": "test", "tags": []}'}}]
    }

    with patch("requests.post", return_value=mock_resp) as mock_post:
        p._chat("data:image/jpeg;base64,abc", "describe this")
        headers = mock_post.call_args.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer sk-test"


@pytest.mark.fast
def test_openai_provider_omits_auth_header_when_no_api_key() -> None:
    """When api_key is None, no Authorization header is included."""
    from unittest.mock import MagicMock, patch

    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "gpt-4o", api_key=None)

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": '{"description": "test", "tags": []}'}}]
    }

    with patch("requests.post", return_value=mock_resp) as mock_post:
        p._chat("data:image/jpeg;base64,abc", "describe this")
        headers = mock_post.call_args.kwargs.get("headers", {})
        assert "Authorization" not in headers


@pytest.mark.fast
def test_openai_provider_retries_once_on_empty_completion(tmp_path) -> None:
    from unittest.mock import MagicMock, patch

    from PIL import Image

    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    img_path = tmp_path / "img.jpg"
    Image.new("RGB", (16, 16), color=(255, 0, 0)).save(img_path)

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "gpt-4o", api_key=None)

    empty_resp = MagicMock()
    empty_resp.ok = True
    empty_resp.raise_for_status = MagicMock()
    empty_resp.json.return_value = {"choices": [{"message": {"content": ""}}]}

    ok_resp = MagicMock()
    ok_resp.ok = True
    ok_resp.raise_for_status = MagicMock()
    ok_resp.json.return_value = {
        "choices": [{"message": {"content": '{"description": "hi", "tags": ["a"]}'}}]
    }

    with patch("requests.post", side_effect=[empty_resp, ok_resp]) as mock_post:
        out = p.describe(img_path)

    assert out == {"description": "hi", "tags": ["a"]}
    assert mock_post.call_count == 2
