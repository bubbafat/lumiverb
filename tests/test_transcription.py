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
        from src.server.srt import parse_srt_segments
        segments = parse_srt_segments(SAMPLE_SRT)
        assert len(segments) == 3

    def test_timestamps_in_milliseconds(self):
        from src.server.srt import parse_srt_segments
        segments = parse_srt_segments(SAMPLE_SRT)
        seg = segments[0]
        assert seg.start_ms == 5000
        assert seg.end_ms == 10500

    def test_sequence_numbers(self):
        from src.server.srt import parse_srt_segments
        segments = parse_srt_segments(SAMPLE_SRT)
        assert segments[0].index == 1
        assert segments[1].index == 2
        assert segments[2].index == 3

    def test_multiline_text_joined(self):
        from src.server.srt import parse_srt_segments
        segments = parse_srt_segments(SAMPLE_SRT)
        assert "Today we're going to talk about" in segments[1].text
        assert "photography techniques." in segments[1].text

    def test_empty_input(self):
        from src.server.srt import parse_srt_segments
        assert parse_srt_segments("") == []
        assert parse_srt_segments("   ") == []
        assert parse_srt_segments(None) == []

    def test_dot_separator(self):
        from src.server.srt import parse_srt_segments
        srt = "1\n00:00:01.500 --> 00:00:05.250\nDot timestamps.\n"
        segments = parse_srt_segments(srt)
        assert len(segments) == 1
        assert segments[0].start_ms == 1500
        assert segments[0].end_ms == 5250

    def test_hour_timestamps(self):
        from src.server.srt import parse_srt_segments
        srt = "1\n01:30:00,000 --> 01:30:05,000\nLate in the video.\n"
        segments = parse_srt_segments(srt)
        assert segments[0].start_ms == 5400000  # 1h30m in ms
        assert segments[0].end_ms == 5405000

    def test_skips_empty_text_blocks(self):
        from src.server.srt import parse_srt_segments
        srt = "1\n00:00:01,000 --> 00:00:02,000\n\n\n2\n00:00:03,000 --> 00:00:04,000\nActual text.\n"
        segments = parse_srt_segments(srt)
        # First block has no text, should be skipped
        assert len(segments) == 1
        assert segments[0].text == "Actual text."

    def test_segment_dataclass_fields(self):
        from src.server.srt import SrtSegment
        seg = SrtSegment(index=1, start_ms=0, end_ms=1000, text="hello")
        assert seg.index == 1
        assert seg.start_ms == 0
        assert seg.end_ms == 1000
        assert seg.text == "hello"


@pytest.mark.fast
class TestTimestampConversion:
    """Test _ts_to_ms helper."""

    def test_basic(self):
        from src.server.srt import _ts_to_ms
        assert _ts_to_ms("00:00:01,000") == 1000

    def test_comma_separator(self):
        from src.server.srt import _ts_to_ms
        assert _ts_to_ms("00:00:05,500") == 5500

    def test_dot_separator(self):
        from src.server.srt import _ts_to_ms
        assert _ts_to_ms("00:00:05.500") == 5500

    def test_hours(self):
        from src.server.srt import _ts_to_ms
        assert _ts_to_ms("02:30:15,750") == 9015750


@pytest.mark.fast
class TestMissingTranscriptionCondition:
    """Verify the missing_transcription SQL condition."""

    def test_condition_exists(self):
        from src.server.repository.tenant import MISSING_CONDITIONS
        assert "missing_transcription" in MISSING_CONDITIONS

    def test_condition_checks_has_transcript(self):
        from src.server.repository.tenant import MISSING_CONDITIONS
        cond = MISSING_CONDITIONS["missing_transcription"]
        assert "NOT (a.has_transcript IS NOT NULL)" in cond  # missing: the reconciler's MADE, negated
        assert "media_type = 'video'" in cond
        assert "duration_sec IS NOT NULL" in cond


@pytest.mark.fast
class TestCliConfig:
    """Verify new CLI config fields."""

    def test_the_whisper_model_is_the_accounts_not_the_workers(self):
        from src.client.cli.config import CLIConfig
        from src.shared.producers import PRODUCERS
        assert "whisper_model" not in CLIConfig.model_fields
        assert PRODUCERS["transcript"].defaults["model"] == "small"

    def test_how_many_at_once_is_the_machines_not_the_workers(self):
        """Each AI machine's "at once" (Settings → AI); an older config's key is ignored."""
        from src.client.cli.config import CLIConfig
        assert "transcribe_concurrency" not in CLIConfig.model_fields
        assert CLIConfig.model_validate_json('{"transcribe_concurrency": 3}') == CLIConfig()


@pytest.mark.fast
class TestHasTranscriptFlag:
    """Test has_transcript field on Asset model."""

    def test_field_exists(self):
        from src.server.models.tenant import Asset
        a = Asset.__table__
        assert "has_transcript" in [c.name for c in a.columns]

    def test_default_is_none(self):
        from src.server.models.tenant import Asset
        col = Asset.__table__.c.has_transcript
        assert col.nullable is True


@pytest.mark.fast
class TestRepairSummaryModel:
    """Verify RepairSummary includes missing_transcription."""

    def test_model_has_field(self):
        from src.server.api.routers.assets import RepairSummary
        summary = RepairSummary()
        assert hasattr(summary, "missing_transcription")
        assert summary.missing_transcription == 0


@pytest.mark.fast
class TestTranscriptEndpointEmptySrt:
    """Verify the transcript endpoint accepts empty SRT for no-speech marking."""

    def test_request_model_allows_empty(self):
        from src.server.api.routers.assets import TranscriptSubmitRequest
        req = TranscriptSubmitRequest(srt="", language="en", source="whisper")
        assert req.srt == ""
        assert req.source == "whisper"


# -------------------------------------------------------------------------
# Phase 2 tests: Transcript search infrastructure
# -------------------------------------------------------------------------


@pytest.mark.fast
class TestSearchContextTranscriptType:
    """Verify /v1/query's SearchContext supports transcript hits."""

    def test_transcript_hit(self):
        from src.server.api.routers.query import SearchContext
        ctx = SearchContext(
            score=1.0,
            hit_type="transcript",
            snippet="Hello and welcome to the show.",
            start_ms=5000,
            end_ms=10500,
        )
        assert ctx.hit_type == "transcript"
        assert ctx.snippet == "Hello and welcome to the show."
        assert ctx.start_ms == 5000
        assert ctx.end_ms == 10500

    def test_snippet_and_times_default_to_none(self):
        from src.server.api.routers.query import SearchContext
        ctx = SearchContext(score=0)
        assert ctx.hit_type == "asset"
        assert ctx.snippet is None
        assert ctx.start_ms is None


@pytest.mark.fast
class TestQuickwitTranscriptIndex:
    """Verify QuickwitClient has transcript index methods."""

    def test_transcript_index_id(self):
        from unittest.mock import patch
        with patch("src.server.search.quickwit_client.get_settings") as mock:
            mock.return_value.quickwit_url = "http://localhost:7280"
            mock.return_value.quickwit_enabled = True
            from src.server.search.quickwit_client import QuickwitClient
            qw = QuickwitClient()
            assert qw.tenant_transcript_index_id("tnt_1") == "lumiverb_tenant_tnt_1_transcripts"

    def test_schema_file_exists(self):
        from pathlib import Path
        schema = Path("quickwit/transcript_index_schema.json")
        assert schema.exists()

    def test_schema_has_text_field(self):
        import json
        from pathlib import Path
        schema = json.loads(Path("quickwit/transcript_index_schema.json").read_text())
        fields = {f["name"] for f in schema["doc_mapping"]["field_mappings"]}
        assert "text" in fields
        assert "start_ms" in fields
        assert "end_ms" in fields
        assert "asset_id" in fields
        assert "language" in fields

    def test_default_search_field_is_text(self):
        import json
        from pathlib import Path
        schema = json.loads(Path("quickwit/transcript_index_schema.json").read_text())
        assert "text" in schema["search_settings"]["default_search_fields"]


@pytest.mark.fast
class TestTranscriptDocumentId:
    """Verify transcript document IDs use timestamp-based format."""

    def test_document_id_format(self):
        from src.server.srt import parse_srt_segments
        segments = parse_srt_segments(SAMPLE_SRT)
        seg = segments[0]
        doc_id = f"ast_1_{seg.start_ms}_{seg.end_ms}"
        assert doc_id == "ast_1_5000_10500"

    def test_unique_ids_for_different_segments(self):
        from src.server.srt import parse_srt_segments
        segments = parse_srt_segments(SAMPLE_SRT)
        ids = {f"ast_1_{s.start_ms}_{s.end_ms}" for s in segments}
        assert len(ids) == len(segments)


def test_audio_that_couldnt_be_read_is_tried_again_not_saved_as_silence(tmp_path, monkeypatch):
    """ffmpeg killed by a stop, or the analysis proxy evicted mid-read: not
    known to be silence, so not saved as "no speech" for good (review, Oct 9)."""
    import subprocess
    from unittest.mock import MagicMock

    from src.processing.video.audio import AudioTrack
    from src.producers.transcript.work import transcribe

    source = tmp_path / "a.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr("src.processing.video.audio.audio_tracks", lambda p: [AudioTrack(0, 2, "aac")])
    killed = subprocess.CompletedProcess(args=[], returncode=-15, stdout=b"", stderr=b"Exiting normally, received signal 15.")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: killed)
    assert transcribe(source, MagicMock()) is None
    no_audio = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"",
                                           stderr=b"Output file #0 does not contain any stream")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: no_audio)
    assert transcribe(source, MagicMock()) == ("", "")
