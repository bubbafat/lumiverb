import pytest


@pytest.mark.fast
def test_describe_image_missing_file(tmp_path):
    """describe_image returns empty dict for missing file."""
    from src.workers.vision import describe_image

    result = describe_image(tmp_path / "nonexistent.jpg")
    assert result == {}


@pytest.mark.fast
def test_vision_worker_no_proxy():
    """VisionWorker.process raises ValueError when proxy_key is None."""
    from unittest.mock import MagicMock

    from src.storage.local import LocalStorage
    from src.workers.vision_worker import VisionWorker

    worker = VisionWorker(
        client=MagicMock(),
        storage=MagicMock(spec=LocalStorage),
        once=True,
    )
    with pytest.raises(ValueError, match="no proxy"):
        worker.process({"asset_id": "ast_123", "proxy_key": None})

