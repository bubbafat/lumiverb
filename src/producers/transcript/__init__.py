"""Transcripts of videos, by the transcripts machines (Settings → AI).

The silences skipped (VAD) are found by the scheduler, whichever machine
transcribes the speech (src/processing/workers/transcripts/speech.py)."""

from src.producers.contract import ITS_JOBS, VIDEO, ProducerSpec, Setting
from src.producers.pools import TRANSCRIPTS

PRODUCER = ProducerSpec(
    artifact="transcript", producer="whisper", version="1", media=VIDEO, unit="second", title="Transcripts", order=110,
    # The probe's duration first.
    applies="a.media_type = 'video' AND a.duration_sec IS NOT NULL",
    made="a.has_transcript IS NOT NULL",
    settings=(
        Setting("model", "small", "Model", kind="text", fixed=ITS_JOBS),
        Setting("vad_min_silence_ms", 500, "Shortest silence skipped", minimum=100, maximum=2000, unit="ms"),
    ),
    needs=("analysis_proxy",),
    kind="transcript", flag="missing_transcription", run="src.producers.transcript.work:Transcript",
    pool=TRANSCRIPTS,
)
