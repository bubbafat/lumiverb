"""Every video Lumiverb keeps is one a browser plays (ADR-018 decision 11).

The web player streams a clip's analysis proxy, or its 10-second preview
until the proxy exists (src/server/api/routers/playback.py), and copies
them as they are: nothing re-encodes for a browser. So both are H.264, 8-bit
4:2:0, at most High profile, with AAC at most stereo, in an MP4 with its
index first. HEVC was measured and given up (Robert, 2026-10-14): Firefox
and Linux browsers don't play it, Chrome only where the hardware decodes it.

These render real originals of the kinds that aren't playable themselves
(10-bit HEVC as phones and drones record it, 10-bit 4:2:2 ProRes as cameras
do) through the real preview and proxy, and read what came out.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.processing.ingest import _generate_video_preview
from src.processing.video.analysis_proxy import (
    AnalysisProxySettings,
    build_command,
    render_analysis_proxy,
)

pytestmark = pytest.mark.fast

# What every browser plays: H.264's profiles up to High (High 10, High 4:2:2
# and High 4:4:4 are H.264 too, and no browser plays them).
PLAYABLE_PROFILES = {"Constrained Baseline", "Baseline", "Main", "High"}

ORIGINALS = {
    "h264.mp4": ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac"],
    "hevc-10bit.mov": ["-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
                       "-x265-params", "log-level=error", "-c:a", "aac", "-ac", "6"],
    "prores-422-10bit.mov": ["-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le",
                             "-c:a", "pcm_s16le"],
}


def _original(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                    "-map", "0:v", "-map", "1:a", *ORIGINALS[path.name], str(path)], check=True)
    return path


def _boxes(data: bytes) -> list[str]:
    """The MP4's top-level boxes, in order."""
    found, at = [], 0
    while at + 8 <= len(data):
        size = int.from_bytes(data[at:at + 4], "big")
        if size == 1:
            size = int.from_bytes(data[at + 8:at + 16], "big")
        elif size == 0:
            size = len(data) - at
        found.append(data[at + 4:at + 8].decode("latin-1"))
        at += size
    return found


def _assert_a_browser_plays(path: Path) -> None:
    data = path.read_bytes()
    boxes = _boxes(data)
    assert boxes[0] == "ftyp", boxes
    # Its index (moov) before the pictures (mdat): a browser plays it as it
    # arrives and seeks without reading to the end first.
    assert boxes.index("moov") < boxes.index("mdat"), boxes
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True).stdout)
    assert "mp4" in info["format"]["format_name"].split(",")
    streams = info["streams"]
    assert {s["codec_type"] for s in streams} <= {"video", "audio"}  # no timecode or data tracks
    [video] = [s for s in streams if s["codec_type"] == "video"]
    assert (video["codec_name"], video["pix_fmt"]) == ("h264", "yuv420p")
    assert video["profile"] in PLAYABLE_PROFILES
    audio = [s for s in streams if s["codec_type"] == "audio"]
    assert audio, "the original's sound is kept"
    for track in audio:
        assert (track["codec_name"], track["profile"]) == ("aac", "LC")
        assert track["channels"] <= 2


@pytest.mark.parametrize("name", list(ORIGINALS))
def test_the_preview_is_one_a_browser_plays(tmp_path: Path, name: str) -> None:
    preview = tmp_path / "preview.mp4"
    preview.write_bytes(_generate_video_preview(_original(tmp_path / name)))
    _assert_a_browser_plays(preview)


@pytest.mark.parametrize("name", list(ORIGINALS))
def test_the_analysis_proxy_is_one_a_browser_plays(tmp_path: Path, name: str) -> None:
    proxy = tmp_path / "proxy.mp4"
    render_analysis_proxy(_original(tmp_path / name), proxy)
    _assert_a_browser_plays(proxy)


def test_a_gpu_encoded_proxy_is_the_same_kind(tmp_path: Path) -> None:
    # NVENC isn't on every machine (nor on this one, maybe): what it's asked for.
    cmd = build_command(Path("/in.mov"), Path("/out.mp4"), AnalysisProxySettings(encoder="h264_nvenc"))
    assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"
    assert cmd[cmd.index("-f", cmd.index("-movflags")) + 1] == "mp4"


@pytest.mark.parametrize("encoder", ["libx265", "hevc_nvenc", "hevc_videotoolbox", "libaom-av1"])
def test_the_scheduler_wont_make_proxies_a_browser_cant_play(monkeypatch, tmp_path, encoder) -> None:
    from src.server.scheduler.settings import SettingError, read_settings

    monkeypatch.chdir(tmp_path)  # no .env or .env.local of this checkout's
    monkeypatch.setenv("LUMIVERB_ANALYSIS_PROXY_ENCODER", encoder)
    with pytest.raises(SettingError, match=f"LUMIVERB_ANALYSIS_PROXY_ENCODER: .*'libx264'.*'h264_nvenc'.*{encoder}"):
        read_settings()
    monkeypatch.setenv("LUMIVERB_ANALYSIS_PROXY_ENCODER", "h264_nvenc")
    assert read_settings().machine().analysis_proxy_encoder == "h264_nvenc"
