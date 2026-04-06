"""Tests for video transcription (ADR-013 Phase 1).

Tests SRT segment parsing, has_transcript flag, missing_transcription SQL
condition, REPAIR_TYPES/ENRICH_TYPES, and transcribe_one error handling.
"""

from __future__ import annotations

import pytest


SAMPLE_SRT = """\
1
00:00:05,000 --> 00:00:10,500
Hello and welcome to the show.

2
00:00:12,500 --> 00:00:18,000
Today we're going to talk about
photography techniques.

3
00:01:45,200 --> 00:01:52,800
As you can see here, the lighting
makes all the difference.
"""


@pytest.mark.fast
class TestSrtSegmentParsing:
    """Test parse_srt_segments produces correct structured output."""

    def test_basic_parsing(self):
        from src.core.srt import parse_srt_segments
        segments = parse_srt_segments(SAMPLE_SRT)
        assert len(segments) == 3

    def test_timestamps_in_milliseconds(self):
        from src.core.srt import parse_srt_segments
        segments = parse_srt_segments(SAMPLE_SRT)
        seg = segments[0]
        assert seg.start_ms == 5000
        assert seg.end_ms == 10500

    def test_sequence_numbers(self):
        from src.core.srt import parse_srt_segments
        segments = parse_srt_segments(SAMPLE_SRT)
        assert segments[0].index == 1
        assert segments[1].index == 2
        assert segments[2].index == 3

    def test_multiline_text_joined(self):
        from src.core.srt import parse_srt_segments
        segments = parse_srt_segments(SAMPLE_SRT)
        assert "Today we're going to talk about" in segments[1].text
        assert "photography techniques." in segments[1].text

    def test_empty_input(self):
        from src.core.srt import parse_srt_segments
        assert parse_srt_segments("") == []
        assert parse_srt_segments("   ") == []
        assert parse_srt_segments(None) == []

    def test_dot_separator(self):
        from src.core.srt import parse_srt_segments
        srt = "1\n00:00:01.500 --> 00:00:05.250\nDot timestamps.\n"
        segments = parse_srt_segments(srt)
        assert len(segments) == 1
        assert segments[0].start_ms == 1500
        assert segments[0].end_ms == 5250

    def test_hour_timestamps(self):
        from src.core.srt import parse_srt_segments
        srt = "1\n01:30:00,000 --> 01:30:05,000\nLate in the video.\n"
        segments = parse_srt_segments(srt)
        assert segments[0].start_ms == 5400000  # 1h30m in ms
        assert segments[0].end_ms == 5405000

    def test_skips_empty_text_blocks(self):
        from src.core.srt import parse_srt_segments
        srt = "1\n00:00:01,000 --> 00:00:02,000\n\n\n2\n00:00:03,000 --> 00:00:04,000\nActual text.\n"
        segments = parse_srt_segments(srt)
        # First block has no text, should be skipped
        assert len(segments) == 1
        assert segments[0].text == "Actual text."

    def test_segment_dataclass_fields(self):
        from src.core.srt import SrtSegment
        seg = SrtSegment(index=1, start_ms=0, end_ms=1000, text="hello")
        assert seg.index == 1
        assert seg.start_ms == 0
        assert seg.end_ms == 1000
        assert seg.text == "hello"


@pytest.mark.fast
class TestTimestampConversion:
    """Test _ts_to_ms helper."""

    def test_basic(self):
        from src.core.srt import _ts_to_ms
        assert _ts_to_ms("00:00:01,000") == 1000

    def test_comma_separator(self):
        from src.core.srt import _ts_to_ms
        assert _ts_to_ms("00:00:05,500") == 5500

    def test_dot_separator(self):
        from src.core.srt import _ts_to_ms
        assert _ts_to_ms("00:00:05.500") == 5500

    def test_hours(self):
        from src.core.srt import _ts_to_ms
        assert _ts_to_ms("02:30:15,750") == 9015750


@pytest.mark.fast
class TestMissingTranscriptionCondition:
    """Verify the missing_transcription SQL condition."""

    def test_condition_exists(self):
        from src.repository.tenant import MISSING_CONDITIONS
        assert "missing_transcription" in MISSING_CONDITIONS

    def test_condition_checks_has_transcript(self):
        from src.repository.tenant import MISSING_CONDITIONS
        cond = MISSING_CONDITIONS["missing_transcription"]
        assert "has_transcript IS NULL" in cond
        assert "media_type = 'video'" in cond
        assert "duration_sec IS NOT NULL" in cond


@pytest.mark.fast
class TestTranscriptionTypes:
    """Verify transcribe is in REPAIR_TYPES and ENRICH_TYPES."""

    def test_in_repair_types(self):
        from src.cli.repair import REPAIR_TYPES
        assert "transcribe" in REPAIR_TYPES

    def test_in_enrich_types(self):
        from src.cli.main import ENRICH_TYPES
        assert "transcribe" in ENRICH_TYPES

    def test_enrich_help_mentions_transcribe(self):
        """The enrich command help text should mention transcribe."""
        from src.cli.main import enrich
        assert "transcribe" in enrich.__doc__


@pytest.mark.fast
class TestCliConfig:
    """Verify new CLI config fields."""

    def test_whisper_model_default(self):
        from src.cli.config import CLIConfig
        cfg = CLIConfig()
        assert cfg.whisper_model == "small"

    def test_transcribe_concurrency_default(self):
        from src.cli.config import CLIConfig
        cfg = CLIConfig()
        assert cfg.transcribe_concurrency == 1


@pytest.mark.fast
class TestHasTranscriptFlag:
    """Test has_transcript field on Asset model."""

    def test_field_exists(self):
        from src.models.tenant import Asset
        a = Asset.__table__
        assert "has_transcript" in [c.name for c in a.columns]

    def test_default_is_none(self):
        from src.models.tenant import Asset
        col = Asset.__table__.c.has_transcript
        assert col.nullable is True


@pytest.mark.fast
class TestRepairSummaryModel:
    """Verify RepairSummary includes missing_transcription."""

    def test_model_has_field(self):
        from src.api.routers.assets import RepairSummary
        summary = RepairSummary()
        assert hasattr(summary, "missing_transcription")
        assert summary.missing_transcription == 0


@pytest.mark.fast
class TestTranscribeOneErrors:
    """Test _transcribe_one error handling without actually running Whisper."""

    def test_nonexistent_source_file(self):
        """_transcribe_one should handle missing source gracefully."""
        from pathlib import Path
        from src.cli.repair import _transcribe_one
        # This will fail at ffmpeg since the file doesn't exist
        result = _transcribe_one(Path("/nonexistent/video.mp4"), "small")
        # Should return None (transient failure) or ("", "") (deterministic)
        # Either is acceptable since ffmpeg will error out
        assert result is None or result == ("", "")

    def test_page_missing_accepts_transcription_param(self):
        """_page_missing should accept missing_transcription kwarg."""
        import inspect
        from src.cli.repair import _page_missing
        sig = inspect.signature(_page_missing)
        assert "missing_transcription" in sig.parameters


@pytest.mark.fast
class TestTranscriptEndpointEmptySrt:
    """Verify the transcript endpoint accepts empty SRT for no-speech marking."""

    def test_request_model_allows_empty(self):
        from src.api.routers.assets import TranscriptSubmitRequest
        req = TranscriptSubmitRequest(srt="", language="en", source="whisper")
        assert req.srt == ""
        assert req.source == "whisper"
