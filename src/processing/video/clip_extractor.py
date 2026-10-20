"""A single frame from a video, as a JPEG, with ffmpeg (a scan's video poster, a scene's picture, a face's frame)."""

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.processing.video import ffmpeg_run
from src.shared.io_utils import file_non_empty

_log = logging.getLogger(__name__)
logger = _log

_ZERO_BYTE_STDERR_SUFFIX = "\n[lumiverb] Output file is 0 bytes; treating as failure"
_DEFAULT_STDERR_TAIL_LINES = 40
# A timestamp rounded to the ms is off by half a ms at most; frames are further apart than this.
FRAME_SEEK_SLACK_SEC = 0.001


def _cmd_to_repro(cmd: list[str]) -> str:
    """Render a shell-safe repro command line for copy/paste."""
    return " ".join(shlex.quote(str(c)) for c in cmd)


def _stderr_tail(stderr: str, *, max_lines: int = _DEFAULT_STDERR_TAIL_LINES) -> str:
    if not stderr:
        return ""
    lines = stderr.strip().splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail).strip()


@dataclass(frozen=True)
class FFmpegAttempt:
    cmd: list[str]
    returncode: int
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def repro(self) -> str:
        return _cmd_to_repro(self.cmd)

    def stderr_tail(self, *, max_lines: int = _DEFAULT_STDERR_TAIL_LINES) -> str:
        return _stderr_tail(self.stderr, max_lines=max_lines)


def _run_ffmpeg(cmd: list[str]) -> FFmpegAttempt:
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
    return FFmpegAttempt(cmd=cmd, returncode=int(result.returncode), stderr=result.stderr or "")


def extract_video_frame(source: Path, dest: Path, timestamp: float = 0.0) -> bool:
    """
    Extract a single high-quality JPEG frame from a video at the given timestamp.

    Uses fast-seeking (-ss before -i). Reads only from source; writes only to dest.
    """
    attempt = extract_video_frame_detailed(source, dest, timestamp=timestamp)
    if attempt.ok:
        return True
    _log.warning(
        "FFmpeg frame extraction failed. Repro: %s\n%s",
        attempt.repro,
        attempt.stderr_tail(),
    )
    return False


def extract_video_frame_detailed(
    source: Path, dest: Path, timestamp: float = 0.0
) -> FFmpegAttempt:
    """Like extract_video_frame(), but returns cmd+stderr for diagnostics."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # A timestamp rounded to the ms (a scene's rep_frame_ms) can sit just after
    # the frame it names; seeking there skips that frame, and when it's the
    # last one ffmpeg gets no frame at all (and says only that the mjpeg
    # encoder wouldn't open: "Non full-range YUV is non-standard").
    seek = max(0.0, timestamp - FRAME_SEEK_SLACK_SEC)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *ffmpeg_run.input_args(),
        "-ss",
        str(seek),
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-vf",
        "scale='min(1280,iw)':-2",
        str(dest),
    ]
    attempt = _run_ffmpeg(cmd)
    if attempt.ok and not file_non_empty(dest):
        dest.unlink(missing_ok=True)
        return FFmpegAttempt(
            cmd=cmd,
            returncode=1,
            stderr=(attempt.stderr or "") + _ZERO_BYTE_STDERR_SUFFIX,
        )
    return attempt
