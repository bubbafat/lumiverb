"""Transcripts of videos, by the transcripts machines (Settings → AI).

The silences skipped (VAD) are found by the scheduler, whichever machine
transcribes the speech (src/processing/workers/transcripts/speech.py)."""

from src.producers import names
from src.producers.contract import ITS_JOBS, VIDEO, Data, Field, ProducerSpec, Setting
from src.producers.pools import TRANSCRIPTS
from src.shared.whisper_models import DETECT, LANGUAGE_CHOICES

MAKES = Data(
    names.TRANSCRIPT, "What's said in it, with times.",
    (Field("language", "text"),
     Field("segments", "list", (Field("start_ms", "number"), Field("end_ms", "number"), Field("text", "text")))),
    stored=("assets.has_transcript", "assets.transcript_srt", "transcript_segments"), person=("language", "segments"),
)

PRODUCER = ProducerSpec(
    makes=MAKES, reads=(names.ANALYSIS_PROXY, names.METADATA),
    # Version 2 (Oct 30): what Whisper makes up left out (speech.cleaned), and the Language setting.
    artifact=names.TRANSCRIPT, version="2", media=VIDEO, unit="second", title="Transcripts", order=110,
    # The probe's duration first.
    applies="a.media_type = 'video' AND a.duration_sec IS NOT NULL",
    made="a.has_transcript IS NOT NULL",
    settings=(
        Setting("model", "small", "Model", kind="text", fixed=ITS_JOBS),
        Setting("vad_min_silence_ms", 500, "Shortest silence skipped", minimum=100, maximum=2000, unit="ms"),
        # The language most likely spoken: Whisper hears it unless it's sure of another (child.SURE; a server is
        # asked to hear it). Detect: whatever Whisper detects, as before.
        Setting("language", DETECT, "Language", kind="choice", choices=LANGUAGE_CHOICES),
    ),
    run="src.producers.transcript.work:Transcript",
    pool=TRANSCRIPTS,
)
