"""Unit tests for video preview worker behavior when ffmpeg fails.

These tests exercise scenarios where a queued video-preview job points at a path
that ffmpeg cannot handle (e.g. misclassified asset, file replaced on disk, or
unsupported codec), without relying on a real ffmpeg binary.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.storage.local import LocalStorage
from src.workers.video_preview_worker import VideoPreviewWorker


def _job(root_path: str, rel_path: str, **extra) -> dict:
    return {
        "job_id": "job_test",
        "asset_id": "ast_test",
        "library_id": "lib_test",
        "root_path": root_path,
        "rel_path": rel_path,
        # In the real system this would be video/* for video-preview jobs,
        # but we don't rely on media_type for these tests.
        "media_type": "video/mp4",
        **extra,
    }


@pytest.mark.fast
def test_video_preview_worker_raises_runtimeerror_when_ffmpeg_fails(tmp_path: Path) -> None:
    """If ffmpeg exits non-zero, the worker should raise RuntimeError.

    This simulates a misclassified asset or unsupported/invalid video where
    ffmpeg returns a failure exit code (e.g. 69) and we surface that as a
    RuntimeError rather than crashing the process.
    """
    storage = LocalStorage(data_dir=str(tmp_path / "data"))
    worker = VideoPreviewWorker(client=MagicMock(), storage=storage, tenant_id="t1")

    job = _job(str(tmp_path), "subdir/clip.mp4")

    with patch("src.workers.video_preview_worker.subprocess.run") as mock_run:
        # Simulate ffmpeg exiting with non-zero status.
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=69,
            cmd=["ffmpeg"],
            output="",
            stderr="ffmpeg error",
        )

        with pytest.raises(RuntimeError, match="ffmpeg failed"):
            worker.process(job)


@pytest.mark.fast
def test_video_preview_worker_handles_path_reuse_non_video_file(tmp_path: Path) -> None:
    """Queued job pointing at a non-video file results in a clean RuntimeError.

    This approximates a path-reuse scenario where a job was enqueued when the
    path was a valid video, but by the time the worker runs the file has been
    replaced by something ffmpeg cannot decode (e.g. a RAW still).
    """
    # Create a dummy non-video file at the expected source location.
    root = tmp_path / "media"
    root.mkdir(parents=True, exist_ok=True)
    source = root / "DSC00001.ARW"
    source.write_bytes(b"not a real video file")

    storage = LocalStorage(data_dir=str(tmp_path / "data"))
    worker = VideoPreviewWorker(client=MagicMock(), storage=storage, tenant_id="t1")
    job = _job(str(root), "DSC00001.ARW")

    with patch("src.workers.video_preview_worker.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=69,
            cmd=["ffmpeg"],
            output="",
            stderr="Invalid data found when processing input",
        )

        with pytest.raises(RuntimeError, match="ffmpeg failed"):
            worker.process(job)

