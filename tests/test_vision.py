import pytest


@pytest.mark.fast
def test_describe_image_missing_file(tmp_path):
    """A missing file is the image's problem, not the endpoint's."""
    from src.processing.workers.captions.base import CaptionError
    from src.processing.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "gpt-4o", api_key=None)
    with pytest.raises(CaptionError) as e:
        p.describe(tmp_path / "nonexistent.jpg")
    assert e.value.endpoint_fault is False
