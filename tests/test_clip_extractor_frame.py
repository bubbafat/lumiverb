"""A scene's frame at a timestamp rounded to the ms: rep_frame_ms can sit just
after the frame it names (23366.67 ms is 23367), and when that's a video's
last frame ffmpeg used to get nothing, failing with "Non full-range YUV is
non-standard" from the mjpeg encoder."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.processing.video.clip_extractor import extract_video_frame_detailed


def _limited_range_clip(path: Path) -> Path:
    # 30 fps for 2 s, limited range (tv), as phones and the analysis proxy make
    # them: the last frame is at 1.96667 s, so its rep_frame_ms is 1967.
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-color_range", "tv", str(path)], check=True)
    return path


@pytest.mark.fast
@pytest.mark.parametrize("rep_frame_ms", [0, 1000, 1967])
def test_frame_at_a_rounded_timestamp(tmp_path: Path, rep_frame_ms: int) -> None:
    clip = _limited_range_clip(tmp_path / "clip.mp4")
    dest = tmp_path / "frame.jpg"
    attempt = extract_video_frame_detailed(clip, dest, timestamp=rep_frame_ms / 1000.0)
    assert attempt.ok, attempt.stderr
    assert dest.stat().st_size > 0


@pytest.mark.fast
def test_past_the_end_is_still_no_frame(tmp_path: Path) -> None:
    clip = _limited_range_clip(tmp_path / "clip.mp4")
    dest = tmp_path / "frame.jpg"
    attempt = extract_video_frame_detailed(clip, dest, timestamp=5.0)
    assert not attempt.ok
    assert not dest.exists()
