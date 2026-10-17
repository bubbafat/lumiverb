"""Videos a client sends are checked, then rewritten, before they're kept
(ADR-018 decisions 10 and 11).

Every video Lumiverb keeps is one a browser plays, whoever made it: H.264,
8-bit 4:2:0, at most High profile, limited range; AAC-LC, at most two
channels a track; in an MP4 with its index first. A video preview or an
analysis proxy that arrives is read by ffprobe (a local file, read only as
an MP4 or QuickTime file: nothing else is opened) and refused unless it's
that (NotPlayableError, saying what's wrong). Then it's rewritten without being
decoded, a stream copy: its index first, its one video track and its audio
tracks only, and none of its metadata (a phone's video says where it was
taken, and nothing a client tagged is kept). What's kept is the rewrite,
and its length is the server's measure of it, never a client's word.

This server's own scheduler sends its renders the same way, so every
upload runs this every day (decision 12). Only the containers are read;
decoding a client's pictures (scenes, transcripts, faces) is the
scheduler's, each in an ffmpeg of its own.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.processing.video import ffmpeg_run

# What every browser plays: H.264's profiles up to High (High 10, High 4:2:2
# and High 4:4:4 are H.264 too, and no browser plays them).
PLAYABLE_PROFILES = ("Constrained Baseline", "Baseline", "Main", "High")
# The longest edge a browser's H.264 decoder takes everywhere (level 5.1 is 4096x2304).
MAX_EDGE = 4096
# A file ffprobe can't read in this long isn't one it should read.
PROBE_SEC = 120
# The demuxer an upload is read with: MP4 and QuickTime (its names are "mov,mp4,m4a,3gp,3g2,mj2").
DEMUXER = "mov"


class NotPlayableError(ValueError):
    """Not a video Lumiverb keeps: what's wrong with it, each in a few words."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Kept:
    """What was kept: its SHA-256, and its length as the server measured it."""

    sha256: str
    duration_sec: float


def _input_args() -> list[str]:
    return ["-protocol_whitelist", "file", "-format_whitelist", DEMUXER]


def probe(path: Path) -> dict:
    """ffprobe's account of the file's container and streams. NotPlayableError when
    it isn't an MP4 or QuickTime file it can read."""
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", *_input_args(), "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=PROBE_SEC, check=False)
    except subprocess.TimeoutExpired:
        raise NotPlayableError(["it couldn't be read in time"]) from None
    if done.returncode != 0:
        raise NotPlayableError(["it isn't an MP4 file that can be read"])
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise NotPlayableError(["it isn't an MP4 file that can be read"]) from None


def problems(info: dict) -> list[str]:
    """What keeps a probed video from being one Lumiverb keeps (none: it's one)."""
    found: list[str] = []
    streams = info.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    if len(videos) != 1:
        found.append(f"it has {len(videos)} video tracks, not one")
    for video in videos[:1]:
        if video.get("codec_name") != "h264":
            found.append(f"its video is {video.get('codec_name') or 'unknown'}, not H.264")
        elif video.get("profile") not in PLAYABLE_PROFILES:
            found.append(f"its H.264 profile is {video.get('profile') or 'unknown'}, above High")
        if video.get("pix_fmt") != "yuv420p":
            found.append(f"its pictures are {video.get('pix_fmt') or 'unknown'}, not 8-bit 4:2:0 (yuv420p)")
        if video.get("color_range") == "pc":
            found.append("its pictures are full range, not limited")
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        if not (0 < width <= MAX_EDGE and 0 < height <= MAX_EDGE):
            found.append(f"its pictures are {width}x{height}, past {MAX_EDGE} on an edge")
    for audio in (s for s in streams if s.get("codec_type") == "audio"):
        if (audio.get("codec_name"), audio.get("profile")) != ("aac", "LC"):
            found.append(f"an audio track is {audio.get('codec_name') or 'unknown'}"
                         f"{' ' + audio['profile'] if audio.get('profile') else ''}, not AAC-LC")
        elif int(audio.get("channels") or 0) > 2:
            found.append(f"an audio track has {audio.get('channels')} channels, more than two")
    if _duration(info) is None:
        found.append("it has no length")
    return found


def _duration(info: dict) -> float | None:
    try:
        seconds = float((info.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def rewrite(source: Path, dest: Path) -> None:
    """A stream copy of source at dest (which isn't there yet): an MP4 with
    its index first, its one video track and its audio tracks, and no
    metadata, the file's, each track's or any chapter's."""
    done = ffmpeg_run.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *_input_args(), "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
        "-map_metadata", "-1", "-map_metadata:s:v", "-1", "-map_metadata:s:a", "-1", "-map_chapters", "-1",
        "-fflags", "+bitexact", "-movflags", "+faststart", "-f", "mp4", str(dest)])
    if done.returncode != 0:
        said = done.stderr.strip().splitlines()
        raise NotPlayableError([f"it couldn't be rewritten ({said[-1] if said else 'ffmpeg failed'})"])


def keep(path: Path) -> Kept:
    """Check the video at path and put its rewrite in its place: its
    SHA-256 as kept and its length. NotPlayableError, path as it was, when it isn't
    a video Lumiverb keeps."""
    import hashlib

    info = probe(path)
    wrong = problems(info)
    if wrong:
        raise NotPlayableError(wrong)
    # Beside it, named as it is (the Store's partial files are swept after a crash).
    rewritten = path.with_name(f"{path.stem}-rewritten{path.suffix}")
    try:
        rewrite(path, rewritten)
        again = probe(rewritten)
        if problems(again):  # a stream copy changes no picture or sound: never, but never kept unchecked
            raise NotPlayableError(problems(again))
        rewritten.replace(path)
    finally:
        rewritten.unlink(missing_ok=True)
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    return Kept(hasher.hexdigest(), _duration(again) or 0.0)
