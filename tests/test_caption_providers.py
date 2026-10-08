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
        p._chat("data:image/jpeg;base64,abc", "describe this", p._vision)
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
        p._chat("data:image/jpeg;base64,abc", "describe this", p._vision)
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


def test_openai_provider_sends_the_settings_it_was_given() -> None:
    """The server's producer settings decide the prompt and sampling: what's
    recorded in lineage is what was sent."""
    from unittest.mock import MagicMock, patch

    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider
    from src.shared.producers import effective_settings

    settings = {**effective_settings("vision"), "prompt": "Say what you see.", "temperature": 0.7, "max_tokens": 99}
    p = OpenAICompatibleCaptionProvider("http://x/v1", "m", settings=settings)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
    with patch("src.client.workers.captions.openai_caption.requests.post", return_value=resp) as post:
        p._chat("data:image/jpeg;base64,abc", p._vision["prompt"], p._vision)
    payload = post.call_args.kwargs["json"]
    assert (payload["temperature"], payload["max_tokens"]) == (0.7, 99)
    assert payload["messages"][0]["content"][1]["text"] == "Say what you see."


# ---------------------------------------------------------------------------
# When the model can't describe or read an image, the provider says so, and
# whether the endpoint was at fault: then vision work stops and no clip is
# charged. "" from extract_text only ever means "no text".
# ---------------------------------------------------------------------------


def _image(tmp_path):
    from PIL import Image

    path = tmp_path / "img.jpg"
    Image.new("RGB", (16, 16)).save(path)
    return path


def _http(status: int):
    import requests
    from unittest.mock import MagicMock

    resp = MagicMock(status_code=status, ok=False, text="nope")
    resp.raise_for_status.side_effect = requests.HTTPError(f"{status} Error", response=resp)
    return resp


@pytest.mark.parametrize(("answer", "endpoint_fault"), [
    ("connection", True),
    (503, True),
    (500, True),
    (404, True),  # the model is gone
    (401, True),
    (400, False),  # the endpoint didn't like this image
    ("garbage", False),  # it answered, but not with a description
])
def test_a_failure_says_whether_the_endpoint_was_at_fault(tmp_path, answer, endpoint_fault):
    from unittest.mock import MagicMock, patch

    import requests

    from src.client.workers.captions.base import CaptionError
    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "m", api_key=None)
    if answer == "connection":
        effect = requests.ConnectionError("refused")
    elif answer == "garbage":
        ok = MagicMock(ok=True, status_code=200)
        ok.json.return_value = {"choices": [{"message": {"content": "I can't help with that."}}]}
        effect = None
    else:
        effect = None
    kwargs = {"side_effect": effect} if effect else {"return_value": ok if answer == "garbage" else _http(answer)}
    with patch("requests.post", **kwargs), patch.object(OpenAICompatibleCaptionProvider, "_sleep_with_countdown"):
        with pytest.raises(CaptionError) as e:
            p.describe(_image(tmp_path))
    assert e.value.endpoint_fault is endpoint_fault


def test_ocr_that_fails_raises_instead_of_saying_no_text(tmp_path):
    from unittest.mock import patch

    import requests

    from src.client.workers.captions.base import CaptionError
    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "m", api_key=None)
    with patch("requests.post", side_effect=requests.Timeout()), \
            patch.object(OpenAICompatibleCaptionProvider, "_sleep_with_countdown"):
        with pytest.raises(CaptionError) as e:
            p.extract_text(_image(tmp_path))
    assert e.value.endpoint_fault is True


def test_a_missing_proxy_is_the_images_problem(tmp_path):
    from src.client.workers.captions.base import CaptionError
    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "m", api_key=None)
    for call in (p.describe, p.extract_text):
        with pytest.raises(CaptionError) as e:
            call(tmp_path / "gone.jpg")
        assert e.value.endpoint_fault is False
