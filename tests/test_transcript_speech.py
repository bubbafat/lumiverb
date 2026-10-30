"""Finding the speech before any machine hears it (the transcripts job).

The transcript producer's settings say which silences are skipped (VAD,
vad_min_silence_ms). The worker finds the speech itself, with the code
faster-whisper runs for vad_filter, and sends only the speech to whichever
machine transcribes it; the times that come back are mapped onto the clip.
So a transcript records the settings it was made with on any machine.
"""

from __future__ import annotations

import json
import random
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from src.processing.workers.transcripts import child
from src.processing.workers.transcripts.base import Segment
from src.processing.workers.transcripts.speech import (
    NOT_SPEECH,
    SpeechError,
    SpeechMap,
    cleaned,
    find_speech,
    restore,
    to_srt,
)

FIXTURES = Path(__file__).resolve().parents[1] / "clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/Fixtures"
RATE = 16_000


def _wav(path: Path, samples: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype(np.int16).tobytes())
    return path


def _speech_wav(source: Path, wav: Path) -> Path:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
                    "-ar", "16000", "-ac", "1", "-f", "wav", str(wav)], check=True)
    return wav


@pytest.mark.fast
def test_times_map_back_onto_the_clip_as_faster_whisper_maps_them() -> None:
    from faster_whisper.vad import SpeechTimestampsMap

    rng = random.Random(7)
    for _ in range(200):
        chunks, at = [], rng.randint(0, 50_000)
        for _ in range(rng.randint(1, 6)):
            start = at + rng.randint(0, 40_000)
            end = start + rng.randint(1_000, 80_000)
            chunks.append({"start": start, "end": end})
            at = end
        ours, theirs = SpeechMap(chunks, RATE), SpeechTimestampsMap(chunks, RATE)
        spoken = sum(c["end"] - c["start"] for c in chunks) / RATE
        for t in [rng.uniform(0, spoken) for _ in range(20)] + [c["end"] / RATE for c in chunks]:
            for is_end in (False, True):
                assert ours.original(t, is_end=is_end) == theirs.get_original_time(t, is_end=is_end), (chunks, t)


@pytest.mark.fast
def test_segments_are_restored_start_and_end() -> None:
    # One second of speech at 2 s, then one at 10 s: 1.5 s into the speech is 10.5 s into the clip.
    chunks = [{"start": 2 * RATE, "end": 3 * RATE}, {"start": 10 * RATE, "end": 11 * RATE}]
    assert restore([Segment(0.25, 0.75, " hi"), Segment(1.25, 1.75, " there")], chunks) == [
        Segment(2.25, 2.75, " hi"), Segment(10.25, 10.75, " there")]


@pytest.mark.fast
def test_the_srt_is_written_as_it_always_was() -> None:
    srt = to_srt([Segment(5.0, 10.5, " Hello and welcome."), Segment(11.0, 11.5, "  "),
                  Segment(3725.25, 3727.0, "Later.")])
    # A segment with nothing said keeps its number, as before.
    assert srt == ("1\n00:00:05,000 --> 00:00:10,500\nHello and welcome.\n\n"
                   "3\n01:02:05,250 --> 01:02:07,000\nLater.\n")
    assert to_srt([]) == ""
    # Times to the millisecond, whatever the float: 2.8 s is 02,800, not 02,799.
    assert to_srt([Segment(2.8, 59.999, "x")]) == "1\n00:00:02,800 --> 00:00:59,999\nx\n"


@pytest.mark.fast
def test_the_speech_is_found_in_a_real_clip_and_only_it_is_kept(tmp_path: Path) -> None:
    """faster-whisper's own VAD (Silero, shipped with it): no download, no GPU."""
    wav = _speech_wav(FIXTURES / "transcribe-english-brownfox-480.mov", tmp_path / "all.wav")
    out = tmp_path / "speech.wav"
    chunks = find_speech(wav, out, 500)
    assert chunks and all(c["end"] > c["start"] for c in chunks)
    with wave.open(str(out)) as w:
        kept = w.getnframes()
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (RATE, 1, 2)
        speech = np.frombuffer(w.readframes(kept), dtype=np.int16)
    assert kept == sum(c["end"] - c["start"] for c in chunks)
    # Kept sample for sample: the speech is the clip's, not re-encoded.
    with wave.open(str(wav)) as w:
        whole = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    assert np.array_equal(speech, np.concatenate([whole[c["start"]:c["end"]] for c in chunks]))


@pytest.mark.fast
def test_silence_has_no_speech_and_writes_nothing(tmp_path: Path) -> None:
    wav = _wav(tmp_path / "quiet.wav", np.zeros(RATE * 3))
    out = tmp_path / "speech.wav"
    assert find_speech(wav, out, 500) == []
    assert not out.exists()


@pytest.mark.fast
def test_the_silences_skipped_are_the_settings(tmp_path: Path) -> None:
    """A longer minimum silence keeps more pauses inside the speech."""
    wav = _speech_wav(FIXTURES / "transcribe-english-brownfox-480.mov", tmp_path / "all.wav")
    short = find_speech(wav, tmp_path / "a.wav", 100)
    long = find_speech(wav, tmp_path / "b.wav", 2000)
    assert len(short) >= len(long)


@pytest.mark.fast
def test_audio_that_cant_be_read_says_so(tmp_path: Path) -> None:
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    with pytest.raises(SpeechError):
        find_speech(bad, tmp_path / "speech.wav", 500)


@pytest.mark.fast
def test_the_child_says_what_it_found_as_json(tmp_path: Path) -> None:
    import sys

    wav = _wav(tmp_path / "quiet.wav", np.zeros(RATE))
    proc = subprocess.run([sys.executable, child.__file__, "find-speech", str(wav), str(tmp_path / "s.wav"), "500"],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"chunks": []}
    proc = subprocess.run([sys.executable, child.__file__, "transcribe", str(tmp_path / "missing.wav"),
                           "no-such-model-xyz", "cpu"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
    error = json.loads(proc.stdout)
    assert error["stage"] == "load" and error["error"]


@pytest.mark.ai
def test_finding_the_speech_first_transcribes_as_faster_whisper_does_itself(tmp_path: Path) -> None:
    """The old path (vad_filter on the whole clip) and the new (the speech
    found first, then transcribed without VAD) say the same, at the same times."""
    from faster_whisper import WhisperModel

    wav = _speech_wav(FIXTURES / "transcribe-english-brownfox-480.mov", tmp_path / "all.wav")
    model = WhisperModel("tiny", device="cpu", compute_type="int8")
    old, old_info = model.transcribe(str(wav), vad_filter=True, vad_parameters={"min_silence_duration_ms": 500})
    old = [Segment(s.start, s.end, s.text) for s in old]

    chunks = find_speech(wav, tmp_path / "speech.wav", 500)
    heard = child.transcribe(str(tmp_path / "speech.wav"), "tiny", device="cpu", compute_type="int8")
    new = restore([Segment(s["start"], s["end"], s["text"]) for s in heard["segments"]], chunks)
    assert new == old and heard["language"] == old_info.language
    assert "fox" in to_srt(new).lower()


# ---------------------------------------------------------------------------
# What Whisper makes up, left out (Robert's videos, Oct 30)
# ---------------------------------------------------------------------------


@pytest.mark.fast
@pytest.mark.parametrize("said", [" You", " Thank you.", " Thanks for watching!", " Bye.", " I", " Bye bye!",
                                  " Sous-titres réalisés par la communauté d'Amara.org", " ご視聴ありがとうございました",
                                  " 감사합니다"])
def test_a_stock_line_where_it_wasnt_speech_is_left_out(said):
    # As the trial heard them: "Thank you." where it was 0.82 sure nobody spoke.
    assert cleaned([Segment(0, 1, said, 0.82), Segment(1, 2, " Over here.", 0.05)]) == \
        [Segment(1, 2, " Over here.", 0.05)]
    assert cleaned([Segment(0, 1, said, NOT_SPEECH)]) == []


@pytest.mark.fast
def test_a_stock_line_somebody_said_is_kept():
    # Said (it was sure), or a machine that didn't say how sure: kept, as is anything else it was unsure of.
    kept = [Segment(0, 1, " Thank you.", 0.05), Segment(1, 2, " Bye.", None), Segment(2, 3, " Thank you for the cake.", 0.9)]
    assert cleaned(kept) == kept


@pytest.mark.fast
def test_a_line_said_again_right_after_itself_is_kept_once():
    # A loop ("I'm going to try it." over and over), told apart by its words alone, not its punctuation or case.
    loop = [Segment(0, 2, " I'm going to try it.", 0.7), Segment(2, 4, " I'm going to try it!", 0.7),
            Segment(4, 6, " i'm going to try it", 0.7), Segment(6, 8, " Two.", 0.7), Segment(8, 10, " I'm going to try it.", 0.7)]
    assert [(s.start, s.text) for s in cleaned(loop)] == [(0, " I'm going to try it."), (6, " Two."),
                                                          (8, " I'm going to try it.")]


@pytest.mark.fast
def test_a_sentence_said_over_and_over_in_one_line_is_said_once():
    [s] = cleaned([Segment(0, 30, " That's all for now. Thank you. Thank you. Thank you. Thank you.", 0.2)])
    assert s == Segment(0, 30, " That's all for now. Thank you.", 0.2)
    # Said once each, nothing changes.
    once = Segment(0, 5, " Hi. Say hi to Ludo. Hi.", 0.1)
    assert cleaned([once]) == [once]


class _Info:
    def __init__(self, language: str, probability: float) -> None:
        self.language, self.language_probability = language, probability


class _Whisper:
    """faster-whisper's model as child.transcribe uses it: what it detects, and what it hears in each language."""

    def __init__(self, detected: str, probability: float) -> None:
        self.detected, self.probability, self.asked = detected, probability, []

    def transcribe(self, wav, vad_filter, language=None):
        from types import SimpleNamespace

        self.asked.append(language)
        heard = language or self.detected
        return (iter([SimpleNamespace(start=0.0, end=1.0, text=f" in {heard}", no_speech_prob=0.1)]),
                _Info(heard, 1.0 if language else self.probability))


@pytest.mark.fast
@pytest.mark.parametrize(("detected", "probability", "language", "heard"), [
    ("te", 0.25, "en", "en"),  # unsure of another: the language chosen
    ("ja", 0.87, "en", "en"),  # the trial's surest other language, below SURE
    ("fr", 0.95, "en", "fr"),  # sure of another: kept
    ("en", 0.40, "en", "en"),  # the one chosen, however unsure
    ("te", 0.25, "", "te"),  # Detect: as before
])
def test_the_built_in_hears_the_language_chosen_unless_sure_of_another(monkeypatch, detected, probability, language,
                                                                        heard):
    whisper = _Whisper(detected, probability)
    monkeypatch.setattr(child, "_whisper", lambda *_: whisper)
    out = child.transcribe("speech.wav", "small", language=language)
    assert out == {"segments": [{"start": 0.0, "end": 1.0, "text": f" in {heard}", "no_speech": 0.1}],
                   "language": heard}
    assert whisper.asked == ([None] if heard == detected else [None, language])


@pytest.mark.fast
def test_the_child_takes_the_language_after_the_device(monkeypatch, capsys):
    got = []
    monkeypatch.setattr(child, "transcribe", lambda *a, **k: got.append((a, k)) or {"segments": [], "language": "en"})
    for argv, said in ((["transcribe", "s.wav", "small"], ("auto", "")), (["transcribe", "s.wav", "small", "cpu"], ("cpu", "")),
                       (["transcribe", "s.wav", "small", "cpu", "en"], ("cpu", "en"))):
        assert child.main(argv) == 0
        args, kwargs = got.pop()
        assert (args[2], kwargs["language"]) == said
    capsys.readouterr()
