"""Videos a client sends are checked, then rewritten, before they're kept
(ADR-018 decisions 10 and 11; src/server/uploaded_video.py).

Real videos made with ffmpeg: what a browser plays is kept, as a stream
copy with its index first and none of its metadata; anything else is
refused, saying what's wrong, and nothing of it is kept.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from src.server import uploaded_video
from tests.test_browser_playable_video import _assert_a_browser_plays, _boxes
from tests.videos import made_with, playable

pytestmark = pytest.mark.fast

H264 = ("-c:v", "libx264", "-preset", "ultrafast")


def _at(tmp_path: Path, data: bytes, name: str = "upload.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _refused(tmp_path: Path, data: bytes, *said: str) -> None:
    path = _at(tmp_path, data)
    with pytest.raises(uploaded_video.NotPlayableError) as refused:
        uploaded_video.keep(path)
    for words in said:
        assert any(words in p for p in refused.value.problems), refused.value.problems
    assert path.read_bytes() == data, "a refused upload is left as it was"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["upload.mp4"], "and nothing beside it"


def _tags(path: Path) -> dict:
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags:stream_tags:stream=codec_type:chapter",
         "-of", "json", str(path)], check=True, capture_output=True, text=True).stdout)


@pytest.mark.parametrize("data", [
    pytest.param(lambda: playable(), id="H.264 with AAC"),
    pytest.param(lambda: playable(audio=False), id="no sound"),
    pytest.param(lambda: made_with(*H264, "-pix_fmt", "yuv420p", "-profile:v", "baseline"), id="Baseline"),
    pytest.param(lambda: made_with(*H264, "-pix_fmt", "yuv420p", "-profile:v", "main", container="mov"),
                 id="Main, in a QuickTime file"),
    pytest.param(lambda: made_with(*H264, "-pix_fmt", "yuv420p", audio="-c:a aac -ac 2"), id="stereo"),
])
def test_what_a_browser_plays_is_kept(tmp_path: Path, data) -> None:
    path = _at(tmp_path, data())
    kept = uploaded_video.keep(path)
    assert kept.duration_sec == pytest.approx(1.0, abs=0.1)
    assert kept.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert _boxes(path.read_bytes()).index("moov") < _boxes(path.read_bytes()).index("mdat")
    if any(s["codec_type"] == "audio" for s in _tags(path)["streams"]):
        _assert_a_browser_plays(path)  # (it asks for sound)


@pytest.mark.parametrize("data,said", [
    pytest.param(lambda: made_with("-c:v", "libx265", "-pix_fmt", "yuv420p", "-x265-params", "log-level=error",
                                   "-tag:v", "hvc1"), "not H.264", id="HEVC"),
    pytest.param(lambda: made_with(*H264, "-pix_fmt", "yuv420p10le"), "above High", id="10-bit H.264"),
    pytest.param(lambda: made_with(*H264, "-pix_fmt", "yuv422p"), "not 8-bit 4:2:0", id="4:2:2"),
    pytest.param(lambda: made_with(*H264, "-pix_fmt", "yuvj420p", "-color_range", "pc"), "full range",
                 id="full range"),
    pytest.param(lambda: made_with(*H264, "-pix_fmt", "yuv420p", audio="-c:a aac -ac 6"), "6 channels",
                 id="5.1 sound"),
    pytest.param(lambda: made_with(*H264, "-pix_fmt", "yuv420p", audio="-c:a pcm_s16le", container="mov"),
                 "not AAC-LC", id="PCM sound"),
    pytest.param(lambda: made_with(*H264, "-pix_fmt", "yuv420p", container="mkv"), "isn't an MP4",
                 id="Matroska"),
    pytest.param(lambda: b"not a video at all", "isn't an MP4", id="not a video"),
    pytest.param(lambda: b"#EXTM3U\n#EXT-X-VERSION:3\nfile:///etc/passwd\n", "isn't an MP4", id="a playlist"),
])
def test_anything_else_is_refused_saying_why(tmp_path: Path, data, said: str) -> None:
    _refused(tmp_path, data(), said)


def test_two_video_tracks_are_refused(tmp_path: Path) -> None:
    (tmp_path / "made").mkdir()
    out = tmp_path / "made" / "two.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=1",
                    "-f", "lavfi", "-i", "testsrc=size=160x120:rate=25:duration=1",
                    "-map", "0:v", "-map", "1:v", *H264, "-pix_fmt", "yuv420p", str(out)], check=True)
    sent = tmp_path / "sent"
    sent.mkdir()
    _refused(sent, out.read_bytes(), "2 video tracks")


def test_the_rewrite_keeps_no_metadata_and_puts_its_index_first(tmp_path: Path) -> None:
    # A phone's video says where it was taken; a client can tag anything. Its index last, as a camera writes it.
    source = tmp_path / "tagged.mov"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-map", "0:v", "-map", "1:a", *H264, "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-metadata", "location=+40.6892-074.0445/", "-metadata", "comment=private",
                    "-metadata", "creation_time=2026-10-17T12:00:00Z",
                    "-metadata:s:v", "title=secret-cam", "-metadata:s:a", "handler_name=SecretAudio",
                    "-timecode", "01:00:00:00", str(source)], check=True)
    assert "tmcd" in subprocess.run(["ffprobe", "-v", "error", "-show_streams", str(source)],
                                    capture_output=True, text=True).stdout  # a timecode track, a data track
    path = _at(tmp_path, source.read_bytes())
    kept = uploaded_video.keep(path)
    assert kept.duration_sec == pytest.approx(2.0, abs=0.1)
    data = path.read_bytes()
    boxes = _boxes(data)
    assert boxes[0] == "ftyp" and boxes.index("moov") < boxes.index("mdat")
    for secret in (b"40.6892", b"private", b"secret-cam", b"SecretAudio", b"2026-10-17"):
        assert secret not in data, secret
    tags = _tags(path)
    assert [s["codec_type"] for s in tags["streams"]] == ["video", "audio"]  # the timecode track left out
    assert not tags.get("chapters")
    _assert_a_browser_plays(path)


def test_the_same_video_sent_again_is_kept_the_same(tmp_path: Path) -> None:
    path = _at(tmp_path, playable(2.0))
    first = uploaded_video.keep(path)
    again = uploaded_video.keep(path)
    assert again == first
