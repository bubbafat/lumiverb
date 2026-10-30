"""What runs in a subprocess for transcription: faster-whisper's VAD, and the
built-in Whisper. ONNX Runtime (the VAD) and CTranslate2 hold on to memory,
so each clip's work runs in a process of its own (CLAUDE.md: don't
collapse it).

Run by path, not as a module, so it needs only faster-whisper and numpy,
whatever the worker's install looks like:

    python child.py find-speech IN.wav OUT.wav MIN_SILENCE_MS
        → {"chunks": [{"start": sample, "end": sample}, ...]}; OUT.wav is the
          speech alone (16 kHz mono 16-bit, the clip's samples), written
          only when there is some.
    python child.py transcribe SPEECH.wav MODEL [DEVICE [LANGUAGE]]
        → {"segments": [{"start", "end", "text", "no_speech"}], "language"}:
          Whisper on the speech, without VAD of its own (the speech was
          found already). LANGUAGE ("en"): the language most likely spoken,
          used unless Whisper is SURE it hears another.

Failures print {"error", "stage"} and exit 1; stage "load" is the model not
loading (the machine's trouble, not the clip's). Whisper's weights are in
the models folder: HF_HOME, which it inherits from the scheduler
(src/processing/models_dir.py use_models_dir).
"""

from __future__ import annotations

import json
import sys
import wave

RATE = 16_000
# How sure Whisper must be that it hears another language than the one chosen (the Language setting) to keep it
# (Robert's videos, Oct 30: every one of 39 transcripts in another language was detected at 0.87 or below, and
# nearly all were English, heard as Telugu, Javanese or "Thank you for watching" in Japanese).
SURE = 0.9


def find_speech(wav_in: str, wav_out: str, min_silence_ms: int) -> list[dict]:
    """The speech in wav_in, as faster-whisper's vad_filter finds it."""
    import numpy as np
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    audio = decode_audio(wav_in, sampling_rate=RATE)
    chunks = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=int(min_silence_ms)))
    chunks = [{"start": int(c["start"]), "end": int(c["end"])} for c in chunks]
    if chunks:
        # decode_audio is the 16-bit samples / 32768, so this is them again, exactly.
        pcm = np.round(np.concatenate([audio[c["start"]:c["end"]] for c in chunks]) * 32768.0)
        with wave.open(wav_out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(np.clip(pcm, -32768, 32767).astype("<i2").tobytes())
    return chunks


def transcribe(wav: str, model: str, device: str = "auto", compute_type: str = "auto", language: str = "") -> dict:
    """Whisper on the speech alone, in the language it detects, or in `language` unless it's SURE of another.
    Raises _Failed with the stage it failed at."""
    try:
        whisper = _whisper(model, device, compute_type)
    except Exception as e:  # noqa: BLE001 — any reason it won't load is the machine's
        raise _Failed(f"Couldn't load {model}: {e}", "load") from e
    try:
        segments, info = whisper.transcribe(wav, vad_filter=False)
        if language and info.language != language and info.language_probability < SURE:
            # Nothing's been heard yet (segments is lazy): only the language was detected.
            segments, info = whisper.transcribe(wav, vad_filter=False, language=language)
        heard = [{"start": s.start, "end": s.end, "text": s.text, "no_speech": s.no_speech_prob} for s in segments]
    except Exception as e:  # noqa: BLE001
        raise _Failed(str(e) or type(e).__name__, "transcribe") from e
    return {"segments": heard, "language": info.language or ""}


def _whisper(model: str, device: str, compute_type: str):
    """The model from what's downloaded alone when it's there (HF_HOME): asked
    to load one, faster-whisper would otherwise ask huggingface.co about it
    each time. One not there yet, or that won't load from what's there (half
    a download), is downloaded as before."""
    from faster_whisper import WhisperModel

    try:
        return WhisperModel(model, device=device, compute_type=compute_type, local_files_only=True)
    except Exception:  # noqa: BLE001 — loaded again below, the way it always was
        return WhisperModel(model, device=device, compute_type=compute_type)


class _Failed(Exception):
    def __init__(self, message: str, stage: str) -> None:
        super().__init__(message)
        self.stage = stage


def main(argv: list[str]) -> int:
    try:
        if argv[:1] == ["find-speech"] and len(argv) == 4:
            try:
                out = {"chunks": find_speech(argv[1], argv[2], int(argv[3]))}
            except Exception as e:  # noqa: BLE001
                raise _Failed(str(e) or type(e).__name__, "find-speech") from e
        elif argv[:1] == ["transcribe"] and len(argv) in (3, 4, 5):
            device, language = (argv[3:] + ["auto", ""][len(argv) - 3:])[:2]
            out = transcribe(argv[1], argv[2], device, language=language)
        else:
            raise _Failed(f"usage: child.py find-speech|transcribe …, not {argv}", "usage")
    except _Failed as e:
        print(json.dumps({"error": str(e), "stage": e.stage}))
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
