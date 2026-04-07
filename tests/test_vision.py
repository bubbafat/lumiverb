import pytest


@pytest.mark.fast
def test_describe_image_missing_file(tmp_path):
    """OpenAICompatibleCaptionProvider.describe returns empty dict for missing file."""
    from src.client.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "gpt-4o", api_key=None)
    result = p.describe(tmp_path / "nonexistent.jpg")
    assert result == {}
