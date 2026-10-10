"""Fast tests for caption providers."""

import pytest


@pytest.mark.fast
def test_openai_provider_id():
    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    assert (
        OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "qwen").provider_id
        == "openai_compatible"
    )


@pytest.mark.fast
def test_factory_returns_openai_compatible():
    from src.processing.workers.captions.factory import get_caption_provider
    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = get_caption_provider(
        vision_model_id="qwen3-visioncaption-2b",
        api_url="http://localhost:1234/v1",
        api_key=None,
    )
    assert isinstance(p, OpenAICompatibleCaptionProvider)
    assert p._model == "qwen3-visioncaption-2b"


@pytest.mark.fast
def test_factory_arbitrary_model_id():
    from src.processing.workers.captions.factory import get_caption_provider
    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = get_caption_provider(
        vision_model_id="llava:13b",
        api_url="http://localhost:1234/v1",
        api_key=None,
    )
    assert isinstance(p, OpenAICompatibleCaptionProvider)
    assert p._model == "llava:13b"


@pytest.mark.fast
def test_openai_strips_thinking_blocks():
    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "qwen")
    raw = "<think>some reasoning here</think>A sunset over mountains."
    assert p._strip_thinking(raw) == "A sunset over mountains."


@pytest.mark.fast
def test_openai_provider_sends_auth_header_when_api_key_set() -> None:
    """When api_key is provided, _chat sends Authorization: Bearer <key>."""
    from unittest.mock import MagicMock, patch

    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

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

    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

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

    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

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

    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider
    from src.shared.producers import effective_settings

    settings = {**effective_settings("vision"), "prompt": "Say what you see.", "temperature": 0.7, "max_tokens": 99}
    p = OpenAICompatibleCaptionProvider("http://x/v1", "m", settings=settings)
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
    with patch("src.processing.workers.captions.openai_caption.requests.post", return_value=resp) as post:
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

    from src.processing.workers.captions.base import CaptionError
    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

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

    from src.processing.workers.captions.base import CaptionError
    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "m", api_key=None)
    with patch("requests.post", side_effect=requests.Timeout()), \
            patch.object(OpenAICompatibleCaptionProvider, "_sleep_with_countdown"):
        with pytest.raises(CaptionError) as e:
            p.extract_text(_image(tmp_path))
    assert e.value.endpoint_fault is True


def test_a_missing_proxy_is_the_images_problem(tmp_path):
    from src.processing.workers.captions.base import CaptionError
    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "m", api_key=None)
    for call in (p.describe, p.extract_text):
        with pytest.raises(CaptionError) as e:
            call(tmp_path / "gone.jpg")
        assert e.value.endpoint_fault is False


# ---------------------------------------------------------------------------
# Reading the description out of the model's reply. Prod logged ~0.3% of
# replies as "No JSON object found" (the log cut at 100 chars), so every
# shape a reply can take is read here: whole, fenced, with text after, with
# a stray quote, with a raw newline, and cut off (max_tokens, a tag loop).
# ---------------------------------------------------------------------------


def _parse(raw: str) -> dict:
    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    return OpenAICompatibleCaptionProvider("http://x/v1", "m")._parse_description(raw)


@pytest.mark.fast
@pytest.mark.parametrize("raw", [
    '{"description": "A buffet.", "tags": ["food", "buffet"]}',
    '```json\n{"description": "A buffet.", "tags": ["food", "buffet"]}\n```',
    'Here you go:\n{"description": "A buffet.", "tags": ["food", "buffet"]}\nHope that helps!',
    '{\n  "description": "A buffet.",\n  "tags": ["food", "buffet"]\n}',
], ids=["plain", "fenced", "text around it", "pretty"])
def test_a_whole_reply_is_read(raw):
    assert _parse(raw) == {"description": "A buffet.", "tags": ["food", "buffet"]}


@pytest.mark.fast
def test_a_stray_quote_doesnt_lose_the_reply():
    # One unescaped quote leaves the brace count inside a string to the end.
    raw = '{"description": "A 12" pizza on a table.", "tags": ["pizza", "food"]}'
    assert _parse(raw) == {"description": 'A 12" pizza on a table.', "tags": ["pizza", "food"]}


@pytest.mark.fast
def test_a_newline_inside_a_string_is_read():
    assert _parse('{"description": "A buffet.\nWarm light.", "tags": ["food"]}')["description"] == (
        "A buffet.\nWarm light.")


@pytest.mark.fast
def test_a_reply_cut_off_in_its_tags_keeps_the_description():
    # The shape of prod's two failures: cut after the description.
    raw = ('{"description": "A warmly lit buffet station with \\"Pot Roast\\" signage.", '
           '"tags": ["buffet", "hot food", "red cabbage", "meat')
    assert _parse(raw) == {"description": 'A warmly lit buffet station with "Pot Roast" signage.',
                           "tags": ["buffet", "hot food", "red cabbage"]}


@pytest.mark.fast
def test_a_tag_loop_is_cut_to_ten_different_tags():
    tags = ", ".join(f'"tag{i % 12}"' for i in range(200))
    out = _parse('{"description": "A hallway.", "tags": [' + tags)
    assert out["description"] == "A hallway."
    assert out["tags"] == [f"tag{i}" for i in range(10)]


@pytest.mark.fast
@pytest.mark.parametrize("raw", [
    '{"description": "The image captures a blurry, first-person perspective of a dimly lit',
    "I can't help with that.",
], ids=["cut off in the description", "no JSON"])
def test_a_reply_without_a_whole_description_is_an_error(raw):
    with pytest.raises(ValueError, match="No JSON object found"):
        _parse(raw)


@pytest.mark.fast
def test_the_reply_and_its_finish_reason_are_logged(caplog):
    import logging
    from unittest.mock import MagicMock, patch

    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://x/v1", "m")
    resp = MagicMock(ok=True, status_code=200)
    reply = '{"description": "' + "x" * 3000
    resp.json.return_value = {"choices": [{"message": {"content": reply}, "finish_reason": "length"}]}
    post = "src.processing.workers.captions.openai_caption.requests.post"
    logger_name = "src.processing.workers.captions.openai_caption"
    with patch(post, return_value=resp), caplog.at_level(logging.DEBUG, logger=logger_name):
        assert p._chat("data:image/jpeg;base64,abc", "describe", p._vision) == reply
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug and reply[:2000] in debug[0] and reply[:2001] not in debug[0]
    warning = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warning and "finish_reason=length" in warning[0]

    caplog.clear()
    resp.json.return_value = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    with patch(post, return_value=resp), caplog.at_level(logging.DEBUG, logger=logger_name):
        p._chat("data:image/jpeg;base64,abc", "describe", p._vision)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
