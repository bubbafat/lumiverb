from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.workers.video_vision_worker import VideoVisionWorker


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


@pytest.mark.fast
def test_video_vision_worker_reads_scene_rep_and_updates_scene() -> None:
    client = MagicMock()
    scene = {
        "scene_id": "scn_1",
        "thumbnail_key": "t/lib/scenes/00/ast_1_0000001000.jpg",
        "rep_frame_ms": 1000,
        "start_ms": 0,
        "end_ms": 1500,
        "description": None,
    }
    client.get.side_effect = [
        _Resp({"scenes": [scene]}),
        _Resp({"scenes": [{**scene, "description": "A frame"}]}),
    ]
    client.patch.return_value = _Resp({})
    client.post.return_value = _Resp({})

    artifact_store = MagicMock()
    artifact_store.read_artifact.return_value = b"\xff\xd8\xffscene"
    worker = VideoVisionWorker(client=client, artifact_store=artifact_store, once=True)

    with patch("src.workers.video_vision_worker.get_caption_provider") as mock_factory:
        provider = MagicMock()
        provider.describe.return_value = {"description": "A frame", "tags": ["tag1"]}
        mock_factory.return_value = provider
        result = worker.process(
            {
                "asset_id": "ast_1",
                "media_type": "video",
                "vision_model_id": "test-vision-model",
                "vision_api_url": "http://example/v1",
                "vision_api_key": None,
                "rel_path": "clip.mp4",
            }
        )

    assert result["model_id"] == "test-vision-model"
    artifact_store.read_artifact.assert_called_once_with(
        "t/lib/scenes/00/ast_1_0000001000.jpg",
        asset_id="ast_1",
        artifact_type="scene_rep",
        rep_frame_ms=1000,
    )
    client.patch.assert_called_once()
    client.post.assert_called_once_with("/v1/video/scenes/scn_1/sync", json={"asset_id": "ast_1"})


@pytest.mark.fast
def test_video_vision_worker_temp_file_cleaned_up_on_provider_error() -> None:
    client = MagicMock()
    scene = {
        "scene_id": "scn_1",
        "thumbnail_key": "t/lib/scenes/00/ast_1_0000001000.jpg",
        "rep_frame_ms": 1000,
        "start_ms": 0,
        "end_ms": 1500,
        "description": None,
    }
    client.get.return_value = _Resp({"scenes": [scene]})

    artifact_store = MagicMock()
    artifact_store.read_artifact.return_value = b"\xff\xd8\xffscene"
    worker = VideoVisionWorker(client=client, artifact_store=artifact_store, once=True)

    captured_tmp: list[Path] = []

    def _record_and_raise(path: Path) -> None:
        captured_tmp.append(path)
        raise RuntimeError("describe failed")

    with patch("src.workers.video_vision_worker.get_caption_provider") as mock_factory:
        provider = MagicMock()
        provider.describe.side_effect = _record_and_raise
        mock_factory.return_value = provider
        with pytest.raises(RuntimeError, match="describe failed"):
            worker.process(
                {
                    "asset_id": "ast_1",
                    "media_type": "video",
                    "vision_model_id": "test-vision-model",
                    "vision_api_url": "http://example/v1",
                    "vision_api_key": None,
                    "rel_path": "clip.mp4",
                }
            )

    assert len(captured_tmp) == 1
    assert not captured_tmp[0].exists()
