"""Transcripts of videos, by the transcripts machines (Settings → AI).

The silences skipped (VAD) are found by the scheduler, whichever machine
transcribes the speech (src/client/workers/transcripts/speech.py)."""

from src.producers.contract import VIDEO, ProducerSpec

PRODUCER = ProducerSpec(
    artifact="transcript", producer="whisper", version="1", media=VIDEO, title="Transcripts", order=110,
    # The probe's duration first.
    applies="a.media_type = 'video' AND a.duration_sec IS NOT NULL",
    made="a.has_transcript IS NOT NULL",
    defaults={"model": "small", "vad_min_silence_ms": 500}, job="transcripts", needs=("analysis_proxy",),
    kind="transcript", flag="missing_transcription", run="src.server.scheduler.runners:transcript", pool="transcripts",
    per_account=True,
)
