"""What producers make of one clip (src/producers/<artifact>/work.py): the
pieces their make() is built from, without the runner.

Anything that touches a real GPU model, ffmpeg or Whisper is mocked at its
module entry point: these stay fast.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.processing.video.audio import AudioTrack
from src.processing.workers.transcripts.base import Heard, Segment, TranscriptError
from src.processing.workers.transcripts.speech import SpeechError
from src.producers.clip.work import embed
from src.producers.ocr.work import read_text
from src.producers.transcript.work import transcribe

pytestmark = pytest.mark.fast


def _jpeg(size: int = 8) -> bytes:
    from PIL import Image as PILImage

    buf = io.BytesIO()
    PILImage.new("RGB", (size, size), (255, 0, 0)).save(buf, format="JPEG")
    return buf.getvalue()


def _cache(data: bytes | None) -> MagicMock:
    cache = MagicMock()
    cache.get.return_value = data
    return cache


# ---- CLIP ------------------------------------------------------------------


def test_an_embedding_without_a_proxy_is_nothing() -> None:
    clip = MagicMock(model_id="clip", model_version="1")
    assert embed(asset_id="a1", rel_path="x.jpg", clip_provider=clip, proxy_cache=_cache(None)) is None
    assert embed(asset_id="a1", rel_path="x.jpg", clip_provider=clip, proxy_cache=None) is None
    clip.embed_image.assert_not_called()


def test_an_embedding_is_its_proxys_vector() -> None:
    clip = MagicMock(model_id="clip", model_version="1")
    clip.embed_image.return_value = [0.1] * 4
    assert embed(asset_id="a2", rel_path="hello.jpg", clip_provider=clip, proxy_cache=_cache(_jpeg())) == {
        "asset_id": "a2", "model_id": "clip", "model_version": "1", "vector": [0.1] * 4}


# ---- OCR -------------------------------------------------------------------


def test_text_is_read_from_the_proxy() -> None:
    ocr = MagicMock()
    ocr.extract_text.return_value = "STOP"
    assert read_text(asset_id="a", rel_path="x.jpg", ocr_provider=ocr, proxy_cache=_cache(_jpeg(4))) == {
        "asset_id": "a", "ocr_text": "STOP"}


def test_no_proxy_reads_no_text() -> None:
    assert read_text(asset_id="a", rel_path="x.jpg", ocr_provider=MagicMock(), proxy_cache=_cache(None)) is None


def test_an_image_that_cant_be_read_is_nothing_made_but_the_machines_fault_says_so() -> None:
    from src.processing.workers.captions.base import CaptionError

    ocr = MagicMock()
    ocr.extract_text.side_effect = RuntimeError("model exploded")
    assert read_text(asset_id="a", rel_path="x.jpg", ocr_provider=ocr, proxy_cache=_cache(_jpeg(4))) is None
    ocr.extract_text.side_effect = CaptionError("refused", endpoint_fault=True)
    with pytest.raises(CaptionError):
        read_text(asset_id="a", rel_path="x.jpg", ocr_provider=ocr, proxy_cache=_cache(_jpeg(4)))


def test_this_machines_trouble_reading_text_goes_up() -> None:
    """OSError (the disk) is a crash for the runner, not the guard's to judge."""
    cache = _cache(_jpeg(4))
    cache.get.side_effect = OSError(5, "Input/output error")
    with pytest.raises(OSError):
        read_text(asset_id="a", rel_path="x.jpg", ocr_provider=MagicMock(), proxy_cache=cache)


def test_no_text_is_empty_text() -> None:
    ocr = MagicMock()
    ocr.extract_text.return_value = None
    assert read_text(asset_id="a", rel_path="x.jpg", ocr_provider=ocr, proxy_cache=_cache(_jpeg(4))) == {
        "asset_id": "a", "ocr_text": ""}


# ---- Transcripts -----------------------------------------------------------


def _ffmpeg_writes(size: int = 10_000, returncode: int = 0, stderr: bytes = b""):
    """subprocess.run for ffmpeg: writes `size` bytes to the WAV it's told to."""
    def run(cmd, **_kwargs):
        if returncode == 0:
            Path(cmd[-1]).write_bytes(b"\x00" * size)
        return MagicMock(returncode=returncode, stderr=stderr)
    return run


def _hears(heard: Heard | BaseException) -> MagicMock:
    transcriber = MagicMock()
    if isinstance(heard, BaseException):
        transcriber.transcribe.side_effect = heard
    else:
        transcriber.transcribe.return_value = heard
    return transcriber


TRACKS = "src.processing.video.audio.audio_tracks"
FIND = "src.processing.workers.transcripts.speech.find_speech"


@pytest.fixture
def src(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mov"
    path.write_bytes(b"\x00" * 16)
    return path


def test_no_audio_track_is_no_speech(src: Path) -> None:
    transcriber = _hears(Heard())
    with patch(TRACKS, return_value=[]), patch("subprocess.run") as run:
        assert transcribe(src, transcriber) == ("", "")
    run.assert_not_called()
    transcriber.transcribe.assert_not_called()


def test_every_audio_track_is_heard(src: Path) -> None:
    """A lav on its own track is mixed in, not dropped."""
    with patch(TRACKS, return_value=[AudioTrack(0, 2, "aac"), AudioTrack(1, 1, "aac")]), \
         patch("subprocess.run", side_effect=_ffmpeg_writes()) as run, patch(FIND, return_value=[]):
        assert transcribe(src, _hears(Heard())) == ("", "")
    assert "amix=inputs=2" in " ".join(run.call_args_list[0].args[0])


def test_audio_that_cant_be_read_is_tried_again_later(src: Path) -> None:
    """A failed probe isn't recorded as 'no speech': its next turn tries again."""
    import subprocess

    with patch(TRACKS, side_effect=subprocess.CalledProcessError(1, "ffprobe")):
        assert transcribe(src, _hears(Heard())) is None


def test_this_machines_trouble_reading_audio_goes_up(src: Path) -> None:
    """No ffprobe or ffmpeg, or a disk error: OSError goes up (the runner's
    crash, uncharged), never the clip's 'tried again later'."""
    with patch(TRACKS, side_effect=FileNotFoundError(2, "ffprobe")), pytest.raises(FileNotFoundError):
        transcribe(src, _hears(Heard()))
    with patch(TRACKS, return_value=[AudioTrack(0, 2, "aac")]), \
         patch("subprocess.run", side_effect=OSError(5, "Input/output error")), pytest.raises(OSError):
        transcribe(src, _hears(Heard()))


def test_ffmpeg_failing_isnt_silence(src: Path) -> None:
    with patch(TRACKS, return_value=[AudioTrack(0, 2, "aac")]), \
         patch("subprocess.run", side_effect=_ffmpeg_writes(returncode=1, stderr=b"killed")):
        assert transcribe(src, _hears(Heard())) is None


def test_the_speech_is_found_here_and_a_machine_hears_only_it(src: Path) -> None:
    """The speech is found here with the producer's silences; what the
    machine says is timed in the speech, and comes back timed in the clip."""
    chunks = [{"start": 32_000, "end": 48_000}]  # one second of speech at 2 s
    found = []

    def find_speech(wav, speech, min_silence_ms):
        found.append((wav.name, speech.name, min_silence_ms))
        speech.write_bytes(b"speech")
        return chunks

    transcriber = _hears(Heard([Segment(0.0, 0.8, " hi there")], "english"))
    with patch(TRACKS, return_value=[AudioTrack(0, 2, "aac")]), \
         patch("subprocess.run", side_effect=_ffmpeg_writes()), patch(FIND, side_effect=find_speech):
        out = transcribe(src, transcriber, 700)
    assert found == [("audio.wav", "speech.wav", 700)]
    assert transcriber.transcribe.call_args.args[0].name == "speech.wav"
    assert out == ("1\n00:00:02,000 --> 00:00:02,800\nhi there\n", "en")


def test_nothing_said_needs_no_machine(src: Path) -> None:
    transcriber = _hears(Heard())
    with patch(TRACKS, return_value=[AudioTrack(0, 2, "aac")]), \
         patch("subprocess.run", side_effect=_ffmpeg_writes()), patch(FIND, return_value=[]):
        assert transcribe(src, transcriber) == ("", "")
    transcriber.transcribe.assert_not_called()


def test_speech_that_cant_be_found_is_tried_again_later(src: Path) -> None:
    with patch(TRACKS, return_value=[AudioTrack(0, 2, "aac")]), \
         patch("subprocess.run", side_effect=_ffmpeg_writes()), patch(FIND, side_effect=SpeechError("boom")):
        assert transcribe(src, _hears(Heard())) is None


def test_a_machines_failure_says_whose_it_is(src: Path) -> None:
    with patch(TRACKS, return_value=[AudioTrack(0, 2, "aac")]), \
         patch("subprocess.run", side_effect=_ffmpeg_writes()), \
         patch(FIND, return_value=[{"start": 0, "end": 16_000}]), pytest.raises(TranscriptError) as e:
        transcribe(src, _hears(TranscriptError("refused", endpoint_fault=True)))
    assert e.value.endpoint_fault


def test_a_wav_too_short_to_hold_speech_is_empty(src: Path) -> None:
    with patch(TRACKS, return_value=[AudioTrack(0, 2, "aac")]), \
         patch("subprocess.run", side_effect=_ffmpeg_writes(size=10)):
        assert transcribe(src, _hears(Heard())) == ("", "")
