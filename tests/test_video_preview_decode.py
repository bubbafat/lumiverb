"""How the scan's 10-second preview decodes its original (src/processing/
ingest.py _generate_video_preview): on a Mac's media engine where there is
one, else on the CPU; again on the CPU when the hardware fails at it, and
again without audio when a track is broken; through ffmpeg_run, so it's
stopped only when stuck. A Linux GPU's decoder is left to the analysis
proxies (its turns keep room for the models)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.processing.ingest import _generate_video_preview
from src.processing.video import ffmpeg_run

pytestmark = pytest.mark.fast


def _clip(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=30:duration=2", "-f", "lavfi", "-i", "sine=duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)], check=True)
    return path


def _ran(monkeypatch, *, engine: str | None, fails=lambda cmd: False) -> list[list[str]]:
    """The commands the preview runs (for real, unless `fails` says this one fails)."""
    ran: list[list[str]] = []
    real = ffmpeg_run.run

    def run(cmd, **kwargs):
        ran.append(cmd)
        if fails(cmd):
            return ffmpeg_run.Finished(1, "Error while decoding")
        return real(cmd, **kwargs)

    monkeypatch.setattr(ffmpeg_run, "decoded_by", lambda choice: engine)
    monkeypatch.setattr(ffmpeg_run, "run", run)
    return ran


def _hwaccel(cmd: list[str]) -> str | None:
    return cmd[cmd.index("-hwaccel") + 1] if "-hwaccel" in cmd else None


def test_a_macs_media_engine_decodes_it(tmp_path, monkeypatch):
    ran = _ran(monkeypatch, engine="videotoolbox")
    assert _generate_video_preview(_clip(tmp_path / "in.mov"))[4:8] == b"ftyp"
    assert [_hwaccel(c) for c in ran] == ["videotoolbox"]
    assert ran[0].index("-hwaccel") < ran[0].index("-i")


def test_one_the_hardware_fails_at_is_made_on_the_cpu(tmp_path, monkeypatch):
    ran = _ran(monkeypatch, engine="videotoolbox", fails=lambda cmd: "-hwaccel" in cmd)
    assert _generate_video_preview(_clip(tmp_path / "in.mov"))[4:8] == b"ftyp"
    assert [(_hwaccel(c), "-an" not in c) for c in ran] == [("videotoolbox", True), (None, True)]


def test_a_broken_audio_track_is_left_out(tmp_path, monkeypatch):
    ran = _ran(monkeypatch, engine=None, fails=lambda cmd: "-an" not in cmd)
    assert _generate_video_preview(_clip(tmp_path / "in.mov"))[4:8] == b"ftyp"
    assert [(_hwaccel(c), "-an" in c) for c in ran] == [(None, False), (None, True)]


def test_a_linux_gpu_is_left_to_the_analysis_proxies(tmp_path, monkeypatch):
    ran = _ran(monkeypatch, engine="vulkan")
    _generate_video_preview(_clip(tmp_path / "in.mov"))
    assert [_hwaccel(c) for c in ran] == [None]


def test_one_that_cant_be_made_says_why(tmp_path, monkeypatch):
    _ran(monkeypatch, engine="videotoolbox", fails=lambda cmd: True)
    with pytest.raises(RuntimeError, match="ffmpeg made no preview of .*in.mov: Error while decoding"):
        _generate_video_preview(_clip(tmp_path / "in.mov"))


def test_a_stuck_one_is_stopped_not_waited_on_forever(tmp_path, monkeypatch):
    def stuck(cmd, **kwargs):
        raise ffmpeg_run.StalledError("ffmpeg made no progress for 300 s")

    monkeypatch.setattr(ffmpeg_run, "decoded_by", lambda choice: None)
    monkeypatch.setattr(ffmpeg_run, "run", stuck)
    with pytest.raises(ffmpeg_run.StalledError):
        _generate_video_preview(_clip(tmp_path / "in.mov"))
