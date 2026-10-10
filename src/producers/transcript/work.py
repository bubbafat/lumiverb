"""A video's transcript, from its analysis proxy: the speech found here, heard
by whichever of the account's transcripts machines is free."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from src.producers.runner import Failed, Work

logger = logging.getLogger(__name__)

# Saved with a transcript nobody could make sense of; the log says more.
COULDNT = "transcription failed (see the log)"


def transcribe(source_path: Path, transcriber, vad_min_silence_ms: int = 500) -> tuple[str, str] | None:
    """Every audio track mixed into a 16 kHz mono WAV, its speech found here
    with the producer's VAD setting, and only the speech heard by
    transcriber. Times come back onto the clip.

    Returns (srt_text, language); srt_text is "" when nothing is said. None
    when it couldn't be tried this time (the audio tracks couldn't be read,
    finding the speech failed). Raises TranscriptError when the machines
    couldn't hear it (it says whose fault), and OSError when this machine
    can't (no ffmpeg, a disk error): a crash, not the clip's."""
    from src.processing.video.audio import audio_tracks, speech_wav_command
    from src.processing.workers.transcripts.speech import SpeechError, find_speech, restore, to_srt
    from src.shared.whisper_models import language_code

    try:
        tracks = audio_tracks(source_path)
    except (subprocess.SubprocessError, ValueError) as exc:
        logger.warning("Couldn't read the audio tracks of %s; trying again later: %s", source_path, exc)
        return None
    if not tracks:
        logger.info("No audio track in %s", source_path)
        return ("", "")

    with tempfile.TemporaryDirectory(prefix="lumiverb-speech-") as tmp:
        # Every audio track mixed: a lav may be on any track.
        wav, speech = Path(tmp) / "audio.wav", Path(tmp) / "speech.wav"
        try:
            result = subprocess.run(speech_wav_command(source_path, wav, tracks), capture_output=True, timeout=1800)
        except subprocess.TimeoutExpired as e:
            logger.warning("Reading the audio of %s failed; trying again later: %s", source_path, e)
            return None
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace") if result.stderr else ""
            if "does not contain any stream" in stderr or "Output file #0 does not contain" in stderr:
                logger.info("No audio track in %s", source_path)
                return ("", "")  # deterministic: no audio
            # Anything else isn't known to be silence: ffmpeg killed by a stop
            # (an update, a reboot), the analysis proxy evicted from the cache
            # mid-read, a stream it couldn't decode. Saving "" would say the
            # clip has no speech, for good; it's tried again later instead
            # (and given up on, if it keeps failing).
            logger.warning("Reading the audio of %s failed (ffmpeg exit %s); trying again later: %s",
                           source_path, result.returncode, stderr[:500])
            return None
        if not wav.exists() or wav.stat().st_size < 1000:  # < 1KB = essentially empty
            logger.info("Audio track too short/empty for %s", source_path)
            return ("", "")
        try:
            chunks = find_speech(wav, speech, vad_min_silence_ms)
        except SpeechError as e:
            logger.warning("Couldn't find the speech in %s; trying again later: %s", source_path, e)
            return None
        if not chunks:
            logger.info("No speech detected in %s", source_path.name)
            return ("", "")
        heard = transcriber.transcribe(speech)

    srt_text = to_srt(restore(heard.segments, chunks))
    language = language_code(heard.language)
    if srt_text:
        logger.info("Transcribed %s: %d chars, language=%s", source_path.name, len(srt_text), language)
    else:
        logger.info("No speech detected in %s", source_path.name)
    return (srt_text, language)


class Transcript(Work):
    artifact = "transcript"

    def __init__(self, acct, job) -> None:
        super().__init__(acct, job)
        self.vad_ms = acct.producers.settings(self.artifact)["vad_min_silence_ms"]
        guard = acct.guard("transcripts")
        self.used = acct.producers.with_model(self.artifact, guard.model)
        self.transcriber = acct.transcriber()

    def make(self, clip: dict) -> tuple[str, str]:
        from src.processing.workers.transcripts.base import TranscriptError

        source = self.acct.analysis_cache.get(clip["asset_id"])
        if source is None:
            raise Failed("its analysis proxy couldn't be read")
        try:
            result = transcribe(source, self.transcriber, self.vad_ms)
        except TranscriptError as e:
            # The machines' trouble stops transcription, charging no clip;
            # the clip's is charged once a check finds the machine fine.
            raise self.machines_or_clip(e) from e
        if result is None:
            raise Failed(COULDNT)
        return result

    def judge(self, clip: dict, error: Exception) -> Exception:
        logger.error("scheduler: transcribing %s failed", clip["rel_path"], exc_info=error)
        return Failed(COULDNT)

    def save(self, client, made: list[tuple[dict, tuple[str, str]]]) -> None:
        for clip, (srt_text, language) in made:
            # An empty transcript says the clip has no speech (has_transcript false).
            client.post(f"/v1/assets/{clip['asset_id']}/transcript", json={
                "srt": srt_text, "language": language, "source": "whisper",
                "lineage": self.lineage(clip, used=self.used)})
