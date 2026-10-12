"""A proxy decoded on hardware has the CPU's pictures, whatever the source's
size (src/processing/video/analysis_proxy.py build_command): hardware hands
frames back in its own layout (a 10-bit clip as p010), and a clip already
within max_edge (not resized) was made 8-bit along another path, so a
640x360 10-bit HEVC clip's proxy differed by machine (the Linux session on
Vulkan, then VideoToolbox here). The frames are first put back into the
source's own format. Runs on this machine's hardware decoder (VideoToolbox
on a Mac, Vulkan on a Linux GPU)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.processing.video import analysis_proxy as AP  # noqa: N812
from src.processing.video import ffmpeg_run
from src.processing.video.analysis_proxy import (
    AnalysisProxySettings,
    build_command,
    render_analysis_proxy,
)

pytestmark = pytest.mark.fast

HARDWARE = ffmpeg_run.decoded_by("auto")
needs_hardware = pytest.mark.skipif(HARDWARE is None, reason="no hardware video decoding here")


def _clip(path: Path, size: str, pix_fmt: str, codec: str = "libx265") -> Path:
    extra = ["-x265-params", "log-level=error"] if codec == "libx265" else []
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size={size}:rate=30:duration=1", "-f", "lavfi", "-i", "sine=duration=1",
                    "-c:v", codec, "-pix_fmt", pix_fmt, *extra, "-c:a", "aac", str(path)], check=True)
    return path


def _frames(path: Path) -> list[str]:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                          "-map", "0:v:0", "-f", "framemd5", "-"], check=True, capture_output=True, text=True).stdout
    return [line.rsplit(",", 1)[1].strip() for line in out.splitlines() if not line.startswith("#")]


def test_hardware_frames_go_back_into_the_sources_format_first():
    cmd = build_command(Path("/in.mov"), Path("/out.mp4"), hwaccel="videotoolbox", pix_fmt="yuv420p10le")
    assert cmd[cmd.index("-vf") + 1].startswith("format=yuv420p10le,scale=")
    # The CPU's own decoding is left as it is: proxies already made stay the same.
    cpu = build_command(Path("/in.mov"), Path("/out.mp4"), pix_fmt="yuv420p10le")
    assert cpu[cpu.index("-vf") + 1].startswith("scale=")
    unknown = build_command(Path("/in.mov"), Path("/out.mp4"), hwaccel="vulkan", pix_fmt=None)
    assert unknown[unknown.index("-vf") + 1].startswith("scale=")


def test_the_sources_format_is_read_from_its_video(tmp_path):
    assert AP.source_pix_fmt(_clip(tmp_path / "ten.mov", "320x180", "yuv420p10le")) == "yuv420p10le"
    assert AP.source_pix_fmt(_clip(tmp_path / "eight.mov", "320x180", "yuv420p", "libx264")) == "yuv420p"
    turned = tmp_path / "turned.mov"  # a portrait phone clip: rotation side data
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90",
                    "-i", str(tmp_path / "ten.mov"), "-c", "copy", str(turned)], check=True)
    assert AP.source_pix_fmt(turned) == "yuv420p10le"
    (tmp_path / "junk.mov").write_bytes(b"not a video")
    assert AP.source_pix_fmt(tmp_path / "junk.mov") is None


@needs_hardware
@pytest.mark.parametrize("size,pix_fmt,codec,turned", [
    ("640x360", "yuv420p10le", "libx265", False),  # within max_edge: not resized (it differed)
    ("640x360", "yuv420p10le", "libx265", True),  # a portrait phone clip
    ("640x360", "yuv420p", "libx265", False),
    ("640x360", "yuv420p", "libx264", False),
    ("1280x720", "yuv420p10le", "libx265", False),  # resized
])
def test_the_hardware_makes_the_cpus_pictures(tmp_path, monkeypatch, size, pix_fmt, codec, turned):
    monkeypatch.setattr(AP, "_gpu_has_room", lambda: True)  # not how full this GPU is right now
    ran: list[list[str]] = []
    real = ffmpeg_run.run

    def run(cmd, **kwargs):
        ran.append(cmd)
        return real(cmd, **kwargs)

    monkeypatch.setattr(ffmpeg_run, "run", run)
    src = _clip(tmp_path / "in.mov", size, pix_fmt, codec)
    if turned:
        flat, src = src, tmp_path / "turned.mov"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90",
                        "-i", str(flat), "-c", "copy", str(src)], check=True)
    hw, cpu = tmp_path / "hw.mp4", tmp_path / "cpu.mp4"
    render_analysis_proxy(src, hw, AnalysisProxySettings(decoder=HARDWARE))
    render_analysis_proxy(src, cpu, AnalysisProxySettings(decoder="cpu"))
    assert [cmd[cmd.index("-hwaccel") + 1] if "-hwaccel" in cmd else None for cmd in ran] == [HARDWARE, None], \
        "the hardware decoded it (no fallback to the CPU)"
    assert _frames(hw) == _frames(cpu)
