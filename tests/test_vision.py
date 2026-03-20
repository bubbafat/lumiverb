import pytest


@pytest.mark.fast
def test_describe_image_missing_file(tmp_path):
    """OpenAICompatibleCaptionProvider.describe returns empty dict for missing file."""
    from src.workers.captions.openai_caption import OpenAICompatibleCaptionProvider

    p = OpenAICompatibleCaptionProvider("http://localhost:1234/v1", "gpt-4o", api_key=None)
    result = p.describe(tmp_path / "nonexistent.jpg")
    assert result == {}


@pytest.mark.fast
def test_vision_worker_no_proxy():
    """VisionWorker.process raises ValueError when proxy_key is None."""
    from unittest.mock import MagicMock

    from src.workers.vision_worker import VisionWorker

    worker = VisionWorker(
        client=MagicMock(),
        artifact_store=MagicMock(),
        once=True,
    )
    with pytest.raises(ValueError, match="proxy_key"):
        worker.process({"asset_id": "ast_123", "proxy_key": None})

