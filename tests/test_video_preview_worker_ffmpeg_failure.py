"""Unit tests for video preview worker behavior when ffmpeg fails.

These tests exercise scenarios where a queued video-preview job points at a path
that ffmpeg cannot handle (e.g. misclassified asset, file replaced on disk, or
unsupported codec), without relying on a real ffmpeg binary.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.storage.artifact_store import ArtifactRef
from src.workers.video_preview_worker import VideoPreviewWorker


def _job(root_path: str, rel_path: str, **extra) -> dict:
    return {
        "job_id": "job_test",
        # Must be a real-ish asset id because LocalStorage buckets by ULID.
        "asset_id": "ast_01ARZ3NDEKTSV4RRFFQ69G5FAV",
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
    artifact_store = MagicMock()
    worker = VideoPreviewWorker(client=MagicMock(), artifact_store=artifact_store)

    job = _job(str(tmp_path), "subdir/clip.mp4")
    (tmp_path / "subdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "subdir" / "clip.mp4").write_bytes(b"not a real mp4, ffmpeg mocked")

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
        artifact_store.write_artifact.assert_not_called()


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

    artifact_store = MagicMock()
    worker = VideoPreviewWorker(client=MagicMock(), artifact_store=artifact_store)
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


@pytest.mark.fast
def test_video_preview_worker_retries_without_audio_when_first_attempt_fails(tmp_path: Path) -> None:
    artifact_store = MagicMock()
    artifact_store.write_artifact.return_value = ArtifactRef(
        key="t1/lib_test/previews/ab/ast_01ARZ3NDEKTSV4RRFFQ69G5FAV_clip.mp4",
        sha256="abc123",
    )
    worker = VideoPreviewWorker(client=MagicMock(), artifact_store=artifact_store)
    job = _job(str(tmp_path), "subdir/clip.mov")
    (tmp_path / "subdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "subdir" / "clip.mov").write_bytes(b"not a real mov, ffmpeg mocked")

    with patch("src.workers.video_preview_worker.subprocess.run") as mock_run:
        calls = {"n": 0}

        def _run_side_effect(cmd, check, capture_output):
            calls["n"] += 1
            if calls["n"] == 1:
                raise subprocess.CalledProcessError(
                    returncode=234,
                    cmd=["ffmpeg", "-c:a", "aac"],
                    output="",
                    stderr="audio decode error",
                )
            Path(cmd[-1]).write_bytes(b"fake mp4 bytes")
            return MagicMock(returncode=0, stdout=b"", stderr=b"")

        mock_run.side_effect = _run_side_effect

        result = worker.process(job)
        assert "video_preview_key" in result
        artifact_store.write_artifact.assert_called_once()
        assert result["video_preview_key"] == "t1/lib_test/previews/ab/ast_01ARZ3NDEKTSV4RRFFQ69G5FAV_clip.mp4"
        write_call = artifact_store.write_artifact.call_args
        assert write_call.args[0] == "video_preview"
        assert write_call.args[1] == "ast_01ARZ3NDEKTSV4RRFFQ69G5FAV"
        assert write_call.kwargs["library_id"] == "lib_test"
        assert write_call.kwargs["rel_path"] == "subdir/clip.mov"

        # Second attempt should include "-an" to disable audio.
        assert mock_run.call_count == 2
        first_cmd = mock_run.call_args_list[0].args[0]
        second_cmd = mock_run.call_args_list[1].args[0]
        assert str(tmp_path / "data") not in first_cmd[-1]
        assert str(tmp_path / "data") not in second_cmd[-1]
        assert first_cmd[-1] == second_cmd[-1]
        assert not Path(first_cmd[-1]).exists()
        assert "-an" in second_cmd


@pytest.mark.fast
def test_video_preview_worker_raises_when_ffmpeg_writes_no_output_file(tmp_path: Path) -> None:
    artifact_store = MagicMock()
    worker = VideoPreviewWorker(client=MagicMock(), artifact_store=artifact_store)
    job = _job(str(tmp_path), "subdir/clip.mov")
    (tmp_path / "subdir").mkdir(parents=True, exist_ok=True)
    (tmp_path / "subdir" / "clip.mov").write_bytes(b"not a real mov, ffmpeg mocked")

    with patch("src.workers.video_preview_worker.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=b"", stderr=b"")
        with pytest.raises(RuntimeError, match="without writing preview output"):
            worker.process(job)
        artifact_store.write_artifact.assert_not_called()

