"""Fast unit tests for EmbedWorker."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.workers.base import BlockJob
from src.workers.embed_worker import EmbedWorker

ASSET_ID = "asset-1"
PROXY_KEY = "proxies/asset-1.jpg"
FAKE_JPEG = b"\xff\xd8\xff" + b"\x00" * 10


def _make_worker(proxy_bytes: bytes = FAKE_JPEG) -> tuple[EmbedWorker, MagicMock]:
    artifact_store = MagicMock()
    artifact_store.read_artifact.return_value = proxy_bytes
    worker = EmbedWorker(client=MagicMock(), artifact_store=artifact_store, once=True)
    return worker, artifact_store


def _image_job(asset_id: str = ASSET_ID, proxy_key: str = PROXY_KEY) -> dict:
    return {
        "job_id": "job-1",
        "asset_id": asset_id,
        "proxy_key": proxy_key,
        "media_type": "image/jpeg",
    }


@pytest.mark.fast
def test_embed_worker_returns_clip_vector() -> None:
    worker, artifact_store = _make_worker()
    fake_vec = [0.1, 0.2, 0.3]

    with patch.object(worker._get_clip().__class__, "embed", return_value=fake_vec):
        with patch("src.workers.embed_worker.CLIPEmbeddingProvider") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.embed.return_value = fake_vec
            mock_instance.model_id = "clip-vit-b32"
            mock_cls.return_value = mock_instance

            result = worker.process(_image_job())

    assert result["embeddings"][0]["vector"] == fake_vec
    artifact_store.read_artifact.assert_called_once_with(
        PROXY_KEY, asset_id=ASSET_ID, artifact_type="proxy"
    )


@pytest.mark.fast
def test_embed_worker_non_image_raises_block_job() -> None:
    worker, _ = _make_worker()
    job = _image_job()
    job["media_type"] = "video/mp4"

    with pytest.raises(BlockJob, match="embed requires an image"):
        worker.process(job)


@pytest.mark.fast
def test_embed_worker_missing_proxy_key_raises_block_job() -> None:
    worker, _ = _make_worker()
    job = _image_job()
    del job["proxy_key"]

    with pytest.raises(BlockJob, match="No proxy_key"):
        worker.process(job)


@pytest.mark.fast
def test_embed_worker_read_failure_raises_block_job() -> None:
    """read_artifact failure (e.g. HTTP 404 in remote mode) → BlockJob, not FileNotFoundError."""
    artifact_store = MagicMock()
    artifact_store.read_artifact.side_effect = FileNotFoundError("not found")
    worker = EmbedWorker(client=MagicMock(), artifact_store=artifact_store, once=True)

    with pytest.raises(BlockJob, match="Could not read proxy"):
        worker.process(_image_job())


@pytest.mark.fast
def test_embed_worker_temp_file_cleaned_up() -> None:
    """Temp file is removed even when embed() raises."""
    worker, _ = _make_worker()
    captured_tmp: list[Path] = []

    def _record_and_raise(path: Path) -> None:
        captured_tmp.append(path)
        raise RuntimeError("embed failed")

    with patch("src.workers.embed_worker.CLIPEmbeddingProvider") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.embed.side_effect = _record_and_raise
        mock_cls.return_value = mock_instance

        with pytest.raises(RuntimeError, match="embed failed"):
            worker.process(_image_job())

    assert len(captured_tmp) == 1
    assert not captured_tmp[0].exists()
