"""Fast unit tests for VideoScanner PTS polling loop.

Verifies that when FFmpeg exits immediately (startup crash) and the PTS queue
stays empty, SyncError is raised quickly — not after the full PTS_QUEUE_TIMEOUT.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

import src.processing.video.video_scanner as vs_module
from src.processing.video.video_scanner import SyncError, VideoScanner

# Tiny frame: 4×4 RGB24
_W, _H = 4, 4
_FRAME_BYTES = bytes(_W * _H * 3)


@pytest.fixture()
def scanner(tmp_path, monkeypatch):
    """VideoScanner pointed at a temp file with a patched _get_video_size."""
    monkeypatch.setattr(vs_module, "PTS_QUEUE_TIMEOUT", 2.0)
    monkeypatch.setattr(vs_module, "PTS_QUEUE_POLL_INTERVAL", 0.1)

    fake_video = tmp_path / "fake.mp4"
    fake_video.touch()

    with patch("src.processing.video.video_scanner._get_video_size", return_value=(_W, _H)):
        yield VideoScanner(fake_video)


@pytest.mark.fast
def test_sync_error_raised_quickly_on_ffmpeg_startup_crash(scanner):
    """FFmpeg exits immediately (poll() → 1) with no PTS output: SyncError
    without waiting out PTS_QUEUE_TIMEOUT (what's asserted is that it wasn't
    waited for, never how fast: a busy machine is slow)."""
    mock_proc = MagicMock()
    mock_proc.stdout.read.return_value = _FRAME_BYTES  # one valid frame read
    mock_proc.poll.return_value = 1  # process already dead
    mock_proc.returncode = 1
    mock_proc.stderr = iter([])  # no stderr lines → PTS queue stays empty

    with patch("subprocess.Popen", return_value=mock_proc):
        start = time.monotonic()
        with pytest.raises(SyncError, match="exited with code 1"):
            list(scanner.scan(0.0, 10.0))
        elapsed = time.monotonic() - start

    from src.processing.video.video_scanner import PTS_QUEUE_TIMEOUT

    assert elapsed < PTS_QUEUE_TIMEOUT / 2, f"waited {elapsed:.2f}s, as if for PTS that can't come"
