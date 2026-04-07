"""Unit tests for SRT parsing utilities."""

from src.server.srt import parse_srt_to_text, validate_srt

SAMPLE_SRT = """\
1
00:00:05,000 --> 00:00:10,000
Hello and welcome to the show.

2
00:00:12,500 --> 00:00:18,000
Today we're going to talk about
photography techniques.

3
00:01:45,000 --> 00:01:52,000
As you can see here, the lighting
makes all the difference.
"""


def test_parse_srt_to_text():
    result = parse_srt_to_text(SAMPLE_SRT)
    assert "Hello and welcome to the show." in result
    assert "photography techniques." in result
    assert "makes all the difference." in result
    # No timestamps or sequence numbers
    assert "-->" not in result
    assert "00:00:05" not in result


def test_parse_srt_multiline():
    result = parse_srt_to_text(SAMPLE_SRT)
    # Multi-line subtitle (entry 2) should be joined
    assert "Today we're going to talk about" in result
    assert "photography techniques." in result


def test_parse_srt_empty():
    assert parse_srt_to_text("") == ""
    assert parse_srt_to_text("   ") == ""
    assert parse_srt_to_text(None) == ""


def test_parse_srt_malformed():
    # No timestamps — just plain text, still extracts it
    result = parse_srt_to_text("This is just plain text.\nNo SRT format here.")
    assert "This is just plain text." in result


def test_parse_srt_strips_sequence_numbers():
    result = parse_srt_to_text(SAMPLE_SRT)
    # Sequence numbers like "1", "2", "3" should not appear as standalone words
    words = result.split()
    # The text shouldn't start with a bare digit from sequence numbers
    assert words[0] == "Hello"


def test_parse_srt_comma_and_dot_timestamps():
    """Both comma (SRT standard) and dot (common variant) separators work."""
    srt = """\
1
00:00:01.000 --> 00:00:05.000
Dot-separated timestamps.

2
00:00:06,000 --> 00:00:10,000
Comma-separated timestamps.
"""
    result = parse_srt_to_text(srt)
    assert "Dot-separated" in result
    assert "Comma-separated" in result
    assert "-->" not in result


def test_validate_srt_valid():
    assert validate_srt(SAMPLE_SRT) is True


def test_validate_srt_invalid():
    assert validate_srt("Just some text without timestamps") is False
    assert validate_srt("") is False
    assert validate_srt(None) is False


def test_validate_srt_minimal():
    minimal = "1\n00:00:00,000 --> 00:00:01,000\nHello\n"
    assert validate_srt(minimal) is True
