"""Tests for OCR text extraction and reasoning cleanup."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider


# ---------------------------------------------------------------------------
# _strip_ocr_reasoning tests
# ---------------------------------------------------------------------------

@pytest.fixture
def provider():
    return OpenAICompatibleCaptionProvider(base_url="http://test:1234/v1", model="test")


def test_strip_reasoning_direct_text(provider):
    """Direct OCR text with no reasoning passes through."""
    assert provider._strip_ocr_reasoning("HORSE SOLDIER\nBOURBON") == "HORSE SOLDIER\nBOURBON"


def test_strip_reasoning_none_variants(provider):
    """NONE variants return empty string."""
    assert provider._strip_ocr_reasoning("NONE") == ""
    assert provider._strip_ocr_reasoning("NONE.") == ""
    assert provider._strip_ocr_reasoning("<NONE>") == ""
    assert provider._strip_ocr_reasoning("N/A") == ""


def test_strip_reasoning_preamble(provider):
    """Reasoning preamble is stripped, OCR text is kept."""
    text = (
        "Based on the visual content of the image, I need to scan for text.\n"
        "\n"
        "1.  **Scan the sign:** There is text on the sign.\n"
        "STOP\n"
        "ONE WAY\n"
    )
    result = provider._strip_ocr_reasoning(text)
    assert "STOP" in result
    assert "ONE WAY" in result
    assert "Based on" not in result
    assert "Scan the sign" not in result


def test_strip_reasoning_user_wants(provider):
    """'The user wants...' lines are stripped."""
    text = (
        "The user wants me to identify any visible text in the image.\n"
        "\n"
        "CAUTION\n"
        "WET FLOOR\n"
    )
    result = provider._strip_ocr_reasoning(text)
    assert "CAUTION" in result
    assert "WET FLOOR" in result
    assert "user wants" not in result


def test_strip_reasoning_markdown(provider):
    """Markdown formatting is cleaned up."""
    text = (
        "# Mendocino Terrace\n"
        "## Wine Bar\n"
        "**Chardonnay**\n"
        "- Jordan, Russian River 20\n"
        "- Laird, Carneros 17\n"
    )
    result = provider._strip_ocr_reasoning(text)
    assert "Mendocino Terrace" in result
    assert "Wine Bar" in result
    assert "Chardonnay" in result
    assert "Jordan, Russian River 20" in result
    assert "**" not in result
    assert "#" not in result


def test_strip_reasoning_img_tags(provider):
    """<img>...</img> tags are stripped."""
    text = '<img>A sign with text.</img>\nNON SWINGING GONDOLA\nCAUTION'
    result = provider._strip_ocr_reasoning(text)
    assert "NON SWINGING GONDOLA" in result
    assert "CAUTION" in result
    assert "<img>" not in result


def test_strip_reasoning_analysis_sentences(provider):
    """Analysis sentences are filtered out."""
    text = (
        "There's a sign above a shop entrance.\n"
        "CLOSED\n"
        "Looking closer at the building, it's blue with white letters.\n"
        "OPEN 24 HOURS\n"
    )
    result = provider._strip_ocr_reasoning(text)
    assert "CLOSED" in result
    assert "OPEN 24 HOURS" in result
    assert "Looking closer" not in result
    assert "it's blue" not in result


def test_strip_reasoning_numbered_steps(provider):
    """Numbered analysis steps are skipped."""
    text = (
        "Based on the image:\n"
        "1.  **Left side:** Text on building\n"
        "2.  **Right side:** More text\n"
        "HOTEL\n"
        "VACANCY\n"
    )
    result = provider._strip_ocr_reasoning(text)
    assert "HOTEL" in result
    assert "VACANCY" in result
    assert "Left side" not in result
    assert "Right side" not in result


def test_strip_reasoning_dedup_consecutive_lines(provider):
    """Consecutive duplicate lines beyond 2 are truncated."""
    text = "Guimet\n" * 20
    result = provider._strip_ocr_reasoning(text)
    assert result == "Guimet\nGuimet"


def test_strip_reasoning_dedup_preserves_non_runs(provider):
    """Non-consecutive duplicates and mixed runs are preserved correctly."""
    text = (
        "Bronzes\n"
        "royaux\n"
        "d'Angkor\n"
        "Guimet\n"
        "Guimet\n"
        "Halkus\n"
        "d'argent\n"
    ) + "Guimet\n" * 150
    result = provider._strip_ocr_reasoning(text)
    lines = result.splitlines()
    assert lines == [
        "Bronzes", "royaux", "d'Angkor",
        "Guimet", "Guimet",
        "Halkus", "d'argent",
        "Guimet", "Guimet",
    ]


def test_strip_reasoning_dedup_alternating_pattern(provider):
    """Alternating multi-line patterns beyond 2 repetitions are truncated."""
    text = "A\nB\n" * 50
    result = provider._strip_ocr_reasoning(text)
    assert result == "A\nB\nA\nB"


def test_strip_reasoning_dedup_three_line_pattern(provider):
    """Three-line repeating pattern is truncated to 2 occurrences."""
    text = "X\nY\nZ\n" * 10
    result = provider._strip_ocr_reasoning(text)
    assert result == "X\nY\nZ\nX\nY\nZ"


def test_strip_reasoning_dedup_single_occurrence(provider):
    """Lines that appear only once are not affected."""
    text = "STOP\nONE WAY\nYIELD"
    assert provider._strip_ocr_reasoning(text) == text


def test_strip_reasoning_empty_input(provider):
    """Empty input returns empty string."""
    assert provider._strip_ocr_reasoning("") == ""
    assert provider._strip_ocr_reasoning("   ") == ""


def test_strip_reasoning_bullet_analysis(provider):
    """Bullet-point analysis with bold labels is stripped."""
    text = (
        "The user wants me to extract text.\n"
        "* **Header:** The main title\n"
        "* **Footer:** Small print\n"
        "DISNEY\n"
        "MAGIC KINGDOM\n"
    )
    result = provider._strip_ocr_reasoning(text)
    assert "DISNEY" in result
    assert "MAGIC KINGDOM" in result
    assert "Header" not in result
    assert "Footer" not in result


# ---------------------------------------------------------------------------
# _repair_json tests
# ---------------------------------------------------------------------------


def test_repair_json_valid_passthrough(provider):
    """Valid JSON is unchanged."""
    import json
    raw = '{"description": "A nice photo.", "tags": ["sun", "beach"]}'
    assert json.loads(provider._repair_json(raw)) == json.loads(raw)


def test_repair_json_unescaped_inner_quotes(provider):
    """Unescaped quotes inside string values are escaped."""
    import json
    raw = (
        '{"description": "A sign for "Blaster" and "Buzz Lightyear Astro Blasters"'
        ' with targets.", "tags": ["game", "neon"]}'
    )
    parsed = json.loads(provider._repair_json(raw))
    assert "Blaster" in parsed["description"]
    assert "Buzz Lightyear Astro Blasters" in parsed["description"]
    assert parsed["tags"] == ["game", "neon"]


def test_repair_json_exact_gemini_failure(provider):
    """Exact failing response from Gemini 2.5 Flash Lite."""
    import json
    raw = (
        '{"description": "A black and white, inverted image shows a sign for '
        '"Blaster" and "Buzz Lightyear Astro Blasters" with a row of circular '
        'targets hanging below. The mood is stark and somewhat surreal due to '
        'the inverted colors and the dark background.", "tags": ["inverted image", '
        '"black and white", "Buzz Lightyear", "Astro Blasters", "targets", '
        '"amusement park", "game", "neon", "surreal", "dark"]}'
    )
    parsed = json.loads(provider._repair_json(raw))
    assert "Blaster" in parsed["description"]
    assert len(parsed["tags"]) == 10


def test_repair_json_already_escaped(provider):
    """Already-escaped quotes are not double-escaped."""
    import json
    raw = r'{"description": "She said \"hello\" loudly.", "tags": ["test"]}'
    parsed = json.loads(provider._repair_json(raw))
    assert 'said "hello" loudly' in parsed["description"]


# ---------------------------------------------------------------------------
# extract_text tests
# ---------------------------------------------------------------------------


def _make_test_jpeg(path: Path) -> None:
    """Create a tiny valid JPEG for testing."""
    from PIL import Image
    img = Image.new("RGB", (100, 100), "red")
    img.save(path, format="JPEG")


@patch.object(OpenAICompatibleCaptionProvider, "_chat")
def test_extract_text_direct_response(mock_chat, provider, tmp_path):
    """Direct text response is returned as-is."""
    mock_chat.return_value = "STOP SIGN"
    img = tmp_path / "test.jpg"
    _make_test_jpeg(img)

    result = provider.extract_text(img)
    assert result == "STOP SIGN"


@patch.object(OpenAICompatibleCaptionProvider, "_chat")
def test_extract_text_none_response(mock_chat, provider, tmp_path):
    """NONE response returns empty string."""
    mock_chat.return_value = "NONE"
    img = tmp_path / "test.jpg"
    _make_test_jpeg(img)

    result = provider.extract_text(img)
    assert result == ""


@patch.object(OpenAICompatibleCaptionProvider, "_chat")
def test_extract_text_empty_response(mock_chat, provider, tmp_path):
    """Empty response returns empty string."""
    mock_chat.return_value = ""
    img = tmp_path / "test.jpg"
    _make_test_jpeg(img)

    result = provider.extract_text(img)
    assert result == ""


@patch.object(OpenAICompatibleCaptionProvider, "_chat")
def test_extract_text_reasoning_response(mock_chat, provider, tmp_path):
    """Reasoning-wrapped response has reasoning stripped."""
    mock_chat.return_value = (
        "Based on the visual content, I see a sign.\n"
        "1.  **Main sign:** Large text\n"
        "WELCOME\n"
        "TO DISNEYLAND\n"
    )
    img = tmp_path / "test.jpg"
    _make_test_jpeg(img)

    result = provider.extract_text(img)
    assert "WELCOME" in result
    assert "TO DISNEYLAND" in result
    assert "Based on" not in result


@patch.object(OpenAICompatibleCaptionProvider, "_chat")
def test_extract_text_missing_file(mock_chat, provider):
    """Missing file returns empty string without calling the model."""
    result = provider.extract_text(Path("/nonexistent/file.jpg"))
    assert result == ""
    mock_chat.assert_not_called()


# ---------------------------------------------------------------------------
# _ocr_one tests
# ---------------------------------------------------------------------------

from src.client.cli.repair import _ocr_one


def test_ocr_one_success():
    """Returns result dict with asset_id and ocr_text."""
    mock_provider = MagicMock()
    mock_provider.extract_text.return_value = "HELLO WORLD"

    mock_cache = MagicMock()
    mock_cache.get.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100

    result = _ocr_one(
        asset_id="ast_123",
        rel_path="test.jpg",
        ocr_provider=mock_provider,
        proxy_cache=mock_cache,
    )

    assert result is not None
    assert result["asset_id"] == "ast_123"
    assert result["ocr_text"] == "HELLO WORLD"


def test_ocr_one_no_proxy():
    """Returns None when proxy is not available."""
    mock_cache = MagicMock()
    mock_cache.get.return_value = None

    result = _ocr_one(
        asset_id="ast_123",
        rel_path="test.jpg",
        ocr_provider=MagicMock(),
        proxy_cache=mock_cache,
    )

    assert result is None


def test_ocr_one_no_text_found():
    """Returns empty ocr_text when model finds no text."""
    mock_provider = MagicMock()
    mock_provider.extract_text.return_value = ""

    mock_cache = MagicMock()
    mock_cache.get.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100

    result = _ocr_one(
        asset_id="ast_123",
        rel_path="test.jpg",
        ocr_provider=mock_provider,
        proxy_cache=mock_cache,
    )

    assert result is not None
    assert result["ocr_text"] == ""


def test_ocr_one_provider_exception():
    """Returns None when OCR provider throws."""
    mock_provider = MagicMock()
    mock_provider.extract_text.side_effect = RuntimeError("model crashed")

    mock_cache = MagicMock()
    mock_cache.get.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 100

    result = _ocr_one(
        asset_id="ast_123",
        rel_path="test.jpg",
        ocr_provider=mock_provider,
        proxy_cache=mock_cache,
    )

    assert result is None


# ---------------------------------------------------------------------------
# ProxyCache._ensure_size tests
# ---------------------------------------------------------------------------

from src.client.proxy.proxy_cache import ProxyCache


def _isolated_cache(tmp_path, **kwargs):
    """Create a ProxyCache using a temp dir for test isolation."""
    cache = ProxyCache(**kwargs)
    cache._dir = tmp_path
    return cache


def test_proxy_cache_ensure_size_small_image(tmp_path):
    """Images smaller than max_edge pass through unchanged."""
    cache = _isolated_cache(tmp_path, max_edge=1280)
    original = b"small-image-bytes"
    result = cache._ensure_size(original)
    assert result == original


def test_proxy_cache_get_returns_none_without_sources(tmp_path):
    """get() returns None when no cache, no root_path, no client."""
    cache = _isolated_cache(tmp_path, max_edge=1280)
    result = cache.get("nonexistent_asset", "nonexistent.jpg")
    assert result is None


def test_proxy_cache_put_and_get(tmp_path):
    """put() stores bytes, get() retrieves them."""
    cache = _isolated_cache(tmp_path, max_edge=1280)
    cache.put("ast_1", b"jpeg-data")
    assert cache.get("ast_1") == b"jpeg-data"


def test_proxy_cache_put_from_path(tmp_path):
    """put_from_path generates proxy and caches it."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = _isolated_cache(cache_dir, max_edge=1280)

    from PIL import Image
    img = Image.new("RGB", (100, 100), "red")
    img_path = tmp_path / "test.jpg"
    img.save(img_path, format="JPEG")

    result = cache.put_from_path("ast_1", img_path)
    assert isinstance(result, bytes)
    assert len(result) > 0

    cached = cache.get("ast_1")
    assert cached == result
