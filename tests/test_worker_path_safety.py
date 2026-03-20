"""Unit tests for path traversal protection in worker process() methods.

All workers that construct a source path from job-supplied root_path + rel_path
must reject traversal sequences before touching the filesystem.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock

from src.workers.exif_worker import ExifWorker
from src.workers.proxy import ProxyWorker
from src.workers.video_index_worker import VideoIndexWorker
from src.workers.video_preview_worker import VideoPreviewWorker


def _job(root_path: str, rel_path: str, **extra) -> dict:
    return {
        "job_id": "job_test",
        "asset_id": "ast_test",
        "library_id": "lib_test",
        "root_path": root_path,
        "rel_path": rel_path,
        "media_type": "image",
        **extra,
    }


# ---------------------------------------------------------------------------
# Traversal rejection
# ---------------------------------------------------------------------------

@pytest.mark.fast
def test_proxy_worker_rejects_traversal(tmp_path: Path) -> None:
    worker = ProxyWorker(client=MagicMock(), artifact_store=MagicMock())
    with pytest.raises(ValueError, match="rel_path escapes"):
        worker.process(_job(str(tmp_path), "../../etc/passwd"))


@pytest.mark.fast
def test_exif_worker_rejects_traversal(tmp_path: Path) -> None:
    worker = ExifWorker(client=MagicMock())
    with pytest.raises(ValueError, match="rel_path escapes"):
        worker.process(_job(str(tmp_path), "../../etc/passwd"))


@pytest.mark.fast
def test_video_preview_worker_rejects_traversal(tmp_path: Path) -> None:
    worker = VideoPreviewWorker(client=MagicMock(), artifact_store=MagicMock())
    with pytest.raises(ValueError, match="rel_path escapes"):
        worker.process(_job(str(tmp_path), "../../etc/passwd", media_type="video"))


@pytest.mark.fast
def test_video_index_worker_rejects_traversal(tmp_path: Path) -> None:
    worker = VideoIndexWorker(client=MagicMock())
    with pytest.raises(ValueError, match="rel_path escapes"):
        worker.process(_job(str(tmp_path), "../../etc/passwd", media_type="video"))


# ---------------------------------------------------------------------------
# Valid paths pass the traversal check (fail later with FileNotFoundError,
# proving the guard does not block legitimate rel_paths)
# ---------------------------------------------------------------------------

@pytest.mark.fast
def test_proxy_worker_allows_nested_valid_path(tmp_path: Path) -> None:
    """A nested rel_path within root passes traversal check; raises FileNotFoundError after."""
    worker = ProxyWorker(client=MagicMock(), artifact_store=MagicMock())
    with pytest.raises(FileNotFoundError):
        worker.process(_job(str(tmp_path), "subdir/photo.jpg"))


@pytest.mark.fast
def test_exif_worker_allows_nested_valid_path(tmp_path: Path) -> None:
    worker = ExifWorker(client=MagicMock())
    with pytest.raises(FileNotFoundError):
        worker.process(_job(str(tmp_path), "subdir/photo.jpg"))


@pytest.mark.fast
def test_video_preview_worker_allows_nested_valid_path(tmp_path: Path) -> None:
    worker = VideoPreviewWorker(client=MagicMock(), artifact_store=MagicMock())
    with pytest.raises(FileNotFoundError):
        worker.process(_job(str(tmp_path), "subdir/clip.mp4", media_type="video"))


@pytest.mark.fast
def test_video_index_worker_allows_nested_valid_path(tmp_path: Path) -> None:
    worker = VideoIndexWorker(client=MagicMock())
    with pytest.raises(FileNotFoundError):
        worker.process(_job(str(tmp_path), "subdir/clip.mp4", media_type="video"))
