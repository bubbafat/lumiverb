"""FFmpeg-based video scanner for scene detection.

Outputs raw RGB24 keyframes at proxy resolution with PTS.

See docs/reference/video_scene_segmentation.md for the pipe contract and constants.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Iterator

_log = logging.getLogger(__name__)
logger = _log

OUT_WIDTH = 480
PTS_QUEUE_TIMEOUT = 10.0
PTS_QUEUE_POLL_INTERVAL = 0.5


class SyncError(Exception):
    """Raised when PTS does not arrive within timeout (FFmpeg hung)."""


@dataclass
class RawFrame:
    """Single frame from the scanner: RGB bytes and presentation timestamp in seconds."""

    bytes: bytes
    pts: float
    width: int
    height: int


def _get_video_size(source: Path) -> tuple[int, int]:
    """Return (width, height) from ffprobe. Height is even for scale=-2:720 style."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0",
        str(source),
    ]
    logger.debug("Running: %s", " ".join(str(a) for a in cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    logger.debug("Exited %d: %s", result.returncode, " ".join(str(a) for a in cmd))
    if result.returncode != 0 and result.stderr:
        logger.warning(
            "stderr: %s",
            result.stderr.decode(errors="replace")
            if isinstance(result.stderr, bytes)
            else result.stderr,
        )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(f"Could not get video size for {source}: {result.stderr or result.stdout}")
    parts = [p for p in result.stdout.strip().split(",") if p]
    if len(parts) != 2:
        raise ValueError(f"Unexpected ffprobe output: {result.stdout}")
    w, h = int(parts[0]), int(parts[1])
    # Force even height for RGB24
    if h % 2 != 0:
        h -= 1
    return w, h


def _scaled_height(width: int, height: int, out_width: int = OUT_WIDTH) -> int:
    """Aspect-preserving height for out_width. Result is even."""
    if width <= 0:
        return 0
    h = int(round(height * out_width / width))
    if h % 2 != 0:
        h += 1
    return h


class VideoScanner:
    """
    Scan a video file via a single FFmpeg process.
    Outputs raw RGB24 frames at 1 FPS, scaled to OUT_WIDTH, with PTS from showinfo.
    """

    def __init__(self, source: Path) -> None:
        self._source = Path(source)
        if not self._source.exists():
            raise FileNotFoundError(str(self._source))
        self._width, self._height = _get_video_size(self._source)

    def scan(
        self,
        start_ts: float,
        end_ts: float,
        *,
        hwaccel: bool = True,
    ) -> Iterator[RawFrame]:
        """
        Decode from start_ts to end_ts, yielding RawFrame for each decoded keyframe.

        Raises SyncError if PTS does not arrive within PTS_QUEUE_TIMEOUT.
        """
        frame_size = self._width * self._height * 3
        pts_queue: Queue[float] = Queue()

        def consume_stderr(pipe) -> None:
            pts_re = re.compile(r"pts_time:([\d.]+)")
            for line in pipe:
                if line is None:
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                m = pts_re.search(line)
                if m:
                    try:
                        pts_queue.put(float(m.group(1)))
                    except ValueError:
                        pass

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-ss",
            str(start_ts),
            "-i",
            str(self._source),
            "-vf",
            "select='eq(pict_type\\,I)',showinfo",
            "-vsync",
            "0",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ]
        if hwaccel:
            cmd.insert(4, "-hwaccel")
            cmd.insert(5, "auto")

        logger.debug("Running: %s", " ".join(str(a) for a in cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        assert proc.stderr is not None
        stderr_thread = threading.Thread(target=consume_stderr, args=(proc.stderr,))
        stderr_thread.daemon = True
        stderr_thread.start()

        try:
            while True:
                chunk = proc.stdout.read(frame_size) if proc.stdout else b""
                if len(chunk) < frame_size:
                    if chunk:
                        _log.warning(
                            "VideoScanner: short read %d bytes (expected %d), stopping",
                            len(chunk),
                            frame_size,
                        )
                    break
                deadline = time.monotonic() + PTS_QUEUE_TIMEOUT
                pts = None
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SyncError(
                            f"PTS did not arrive within {PTS_QUEUE_TIMEOUT}s (FFmpeg may have hung)"
                        )
                    try:
                        pts = pts_queue.get(timeout=min(PTS_QUEUE_POLL_INTERVAL, remaining))
                        break
                    except Empty:
                        exit_code = proc.poll()
                        if exit_code is not None:
                            raise SyncError(
                                f"FFmpeg exited with code {exit_code} before PTS arrived"
                            )
                if pts > end_ts:
                    break
                yield RawFrame(
                    bytes=chunk,
                    pts=pts,
                    width=self._width,
                    height=self._height,
                )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            logger.debug("FFmpeg exited %d", proc.returncode)
            stderr_thread.join(timeout=1.0)
