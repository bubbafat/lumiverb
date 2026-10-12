"""Running ffmpeg, stopped only when it's stuck (src/processing/video/ffmpeg_run.py):
one that reports progress finishes however long it takes; one whose progress
stops is stopped and said so. And which decoder this machine reads originals
with. A stand-in ffmpeg (a script writing progress as ffmpeg's -progress
does) plays the slow and the stuck ones; a real one, a real render."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from src.processing.video import ffmpeg_run

pytestmark = pytest.mark.fast


def _fake_ffmpeg(tmp_path: Path, body: str) -> Path:
    """A program ffmpeg_run runs as ffmpeg: `body` is Python; `report(n)`
    writes a progress block for frame n, `pid_file` holds its pid."""
    script = tmp_path / "ffmpeg"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        f"open({str(tmp_path / 'pid')!r}, 'w').write(str(os.getpid()))\n"
        "def report(n):\n"
        "    sys.stdout.write(f'frame={n}\\nout_time_us={n * 33333}\\ntotal_size={n * 100}\\nprogress=continue\\n')\n"
        "    sys.stdout.flush()\n"
        + body
    )
    script.chmod(0o755)
    return script


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_a_slow_one_that_moves_finishes(tmp_path):
    # Three seconds of steady progress against a two-second stall limit: a
    # limit on time would have stopped it; this one isn't.
    fake = _fake_ffmpeg(tmp_path, "for n in range(60):\n    report(n)\n    time.sleep(0.05)\n"
                                  "sys.stderr.write('done')\n")
    finished = ffmpeg_run.run([str(fake), "-i", "in.mov", "out.mp4"], stall_after=2.0)
    assert finished == ffmpeg_run.Finished(0, "done")


def test_one_whose_progress_stops_is_stopped_and_says_so(tmp_path):
    fake = _fake_ffmpeg(tmp_path, "report(1)\ntime.sleep(600)\n")
    with pytest.raises(ffmpeg_run.StalledError, match="ffmpeg made no progress for 1 s"):
        ffmpeg_run.run([str(fake)], stall_after=1.0)
    assert _gone(int((tmp_path / "pid").read_text())), "the stuck ffmpeg is stopped, not left running"


def test_reports_that_say_the_same_thing_arent_progress(tmp_path):
    # ffmpeg keeps writing its report while stuck on a read: the same frame
    # over and over.
    fake = _fake_ffmpeg(tmp_path, "while True:\n    report(7)\n    time.sleep(0.05)\n")
    with pytest.raises(ffmpeg_run.StalledError):
        ffmpeg_run.run([str(fake)], stall_after=1.0)
    assert _gone(int((tmp_path / "pid").read_text()))


def test_its_exit_and_what_it_said_come_back(tmp_path):
    fake = _fake_ffmpeg(tmp_path, "report(1)\nsys.stderr.write('Invalid data found')\nsys.exit(1)\n")
    assert ffmpeg_run.run([str(fake)]) == ffmpeg_run.Finished(1, "Invalid data found")


def test_a_real_render(tmp_path):
    out = tmp_path / "out.mp4"
    finished = ffmpeg_run.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
                               "-i", "testsrc2=size=320x240:rate=30:duration=1", "-c:v", "libx264",
                               "-preset", "ultrafast", str(out)])
    assert finished.returncode == 0 and out.stat().st_size > 0


@pytest.mark.parametrize("choice,vulkan,videotoolbox,expected", [
    ("auto", True, False, "vulkan"),  # a Linux GPU
    ("auto", False, True, "videotoolbox"),  # a Mac
    ("auto", True, True, "vulkan"),
    ("auto", False, False, None),  # the CPU
    ("cpu", True, True, None),
    ("", True, True, None),
    ("cuda", False, False, "cuda"),
    ("videotoolbox", False, False, "videotoolbox"),
])
def test_which_decoder_reads_the_originals(monkeypatch, choice, vulkan, videotoolbox, expected):
    monkeypatch.setattr(ffmpeg_run, "vulkan_decodes", lambda: vulkan)
    monkeypatch.setattr(ffmpeg_run, "videotoolbox_decodes", lambda: videotoolbox)
    assert ffmpeg_run.decoded_by(choice) == expected


def test_a_macs_media_engine_needs_no_room_on_a_gpu():
    assert ffmpeg_run.SHARED_MEMORY == ("videotoolbox",)
