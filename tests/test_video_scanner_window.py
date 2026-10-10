"""VideoScanner.scan(start, end) on a real clip: each frame's pts is the
video's time, not the time since the seek, and only [start, end) is
scanned. FFmpeg's showinfo counts from the -ss seek point, so a later
window used to come back timed from 0 and run on to the clip's end."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.processing.video.video_scanner import VideoScanner


def _clip(path: Path, *, seconds: int, gop: int, ts_offset: float = 0.0) -> Path:
    """testsrc at 30 fps with a keyframe every `gop` frames, its timestamps starting at ts_offset."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=30:duration={seconds}",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0"]
    if ts_offset:
        cmd += ["-output_ts_offset", str(ts_offset)]
    subprocess.run([*cmd, str(path)], check=True)
    return path


def _keyframes(path: Path) -> list[float]:
    """The clip's keyframe times, from its start (ffprobe, independent of the scanner)."""
    def probe(*args: str) -> str:
        return subprocess.run(["ffprobe", "-v", "error", *args, str(path)],
                              capture_output=True, text=True, check=True).stdout
    start = float(probe("-show_entries", "format=start_time", "-of", "csv=p=0").strip() or 0)
    times = probe("-select_streams", "v:0", "-skip_frame", "nokey",
                  "-show_entries", "frame=pts_time", "-of", "csv=p=0").split()
    return [float(t.strip(",")) - start for t in times if t.strip(",")]


def _scan(path: Path, start: float, end: float) -> list[float]:
    return [f.pts for f in VideoScanner(path).scan(start, end, hwaccel=False)]


def _between(times: list[float], start: float, end: float) -> list[float]:
    return [t for t in times if start - 1e-6 <= t < end - 1e-6]


@pytest.mark.fast
def test_a_later_window_is_timed_from_the_videos_start(tmp_path: Path) -> None:
    # 37 frames a keyframe: none falls on the window's start (the first after 20 s is 21.0667).
    clip = _clip(tmp_path / "clip.mp4", seconds=40, gop=37)
    want = _between(_keyframes(clip), 20.0, 30.0)
    assert want and want[0] > 20.0

    got = _scan(clip, 20.0, 30.0)
    assert got == pytest.approx(want, abs=1e-3)
    assert all(20.0 <= t < 30.0 for t in got)


@pytest.mark.fast
def test_windows_end_to_end_see_every_keyframe_once(tmp_path: Path) -> None:
    # A keyframe each second, so one sits exactly on each window's edge.
    clip = _clip(tmp_path / "clip.mp4", seconds=30, gop=30)
    got = [t for start in (0.0, 10.0, 20.0) for t in _scan(clip, start, start + 10.0)]
    assert got == pytest.approx(_keyframes(clip), abs=1e-3)
    assert _scan(clip, 10.0, 20.0)[0] == pytest.approx(10.0, abs=1e-3)


@pytest.mark.fast
def test_a_stream_that_doesnt_start_at_zero(tmp_path: Path) -> None:
    # Its first frame stamped 1.5 s (as MPEG-TS and some cameras' files are): times are still from its start.
    clip = _clip(tmp_path / "clip.mp4", seconds=40, gop=37, ts_offset=1.5)
    got = _scan(clip, 20.0, 30.0)
    assert got == pytest.approx(_between(_keyframes(clip), 20.0, 30.0), abs=1e-3)
    assert all(20.0 <= t < 30.0 for t in got)
