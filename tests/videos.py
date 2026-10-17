"""Small videos of the kind Lumiverb keeps: what a client sends as a video
preview or an analysis proxy, which the server checks and rewrites before
keeping it (src/server/uploaded_video.py). Made once per run with ffmpeg."""

from __future__ import annotations

import functools
import subprocess
import tempfile
from pathlib import Path


@functools.cache
def playable(seconds: float = 1.0, *, audio: bool = True, size: str = "160x120") -> bytes:
    """An MP4 every browser plays: H.264 (8-bit 4:2:0) and AAC, its index first."""
    sound = (["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}"] if audio else [])
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "video.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}", *sound,
                        "-map", "0:v", *(["-map", "1:a", "-c:a", "aac"] if audio else []),
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "50",
                        "-movflags", "+faststart", str(out)], check=True)
        return out.read_bytes()


@functools.cache
def made_with(*args: str, seconds: float = 1.0, audio: str = "-c:a aac", container: str = "mp4") -> bytes:
    """A video made with these ffmpeg video arguments (and audio ones), for what a browser doesn't play."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / f"video.{container}"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc2=size=160x120:rate=25:duration={seconds}",
                        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
                        "-map", "0:v", "-map", "1:a", *args, *audio.split(), str(out)], check=True)
        return out.read_bytes()
