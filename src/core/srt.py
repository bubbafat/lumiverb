"""SRT subtitle format parsing utilities."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Matches SRT timestamp lines: "00:00:05,000 --> 00:00:10,000"
_TIMESTAMP_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}\s*$"
)

# Matches sequence numbers (digits on their own line)
_SEQUENCE_RE = re.compile(r"^\d+\s*$")


def parse_srt_to_text(srt_content: str) -> str:
    """Extract plain text from SRT content, stripping timestamps and indices.

    Returns all subtitle text concatenated with spaces, suitable for
    full-text search indexing.
    """
    if not srt_content or not srt_content.strip():
        return ""

    lines = srt_content.splitlines()
    text_parts: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _SEQUENCE_RE.match(stripped):
            continue
        if _TIMESTAMP_RE.match(stripped):
            continue
        text_parts.append(stripped)

    return " ".join(text_parts)


@dataclass
class SrtSegment:
    """A single SRT subtitle segment with millisecond timestamps."""

    index: int
    start_ms: int
    end_ms: int
    text: str


def _ts_to_ms(ts: str) -> int:
    """Convert SRT timestamp (HH:MM:SS,mmm or HH:MM:SS.mmm) to milliseconds."""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    h, m = int(parts[0]), int(parts[1])
    sec_parts = parts[2].split(".")
    s = int(sec_parts[0])
    ms = int(sec_parts[1]) if len(sec_parts) > 1 else 0
    return h * 3600000 + m * 60000 + s * 1000 + ms


# Matches full timestamp line with capture groups for start and end
_TS_CAPTURE_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})"
)


def parse_srt_segments(srt_content: str) -> list[SrtSegment]:
    """Parse SRT into structured segments with millisecond timestamps.

    Splits on blank lines, extracts sequence number, timestamps, and text.
    Handles both comma and dot timestamp separators.
    """
    if not srt_content or not srt_content.strip():
        return []

    blocks = re.split(r"\n\s*\n", srt_content.strip())
    segments: list[SrtSegment] = []

    for i, block in enumerate(blocks):
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue

        # Find the timestamp line (may be line 0 or 1)
        ts_match = None
        ts_line_idx = -1
        for j, line in enumerate(lines):
            ts_match = _TS_CAPTURE_RE.search(line)
            if ts_match:
                ts_line_idx = j
                break

        if ts_match is None:
            continue

        start_ms = _ts_to_ms(ts_match.group(1))
        end_ms = _ts_to_ms(ts_match.group(2))

        # Text is everything after the timestamp line
        text_lines = lines[ts_line_idx + 1 :]
        text = " ".join(line.strip() for line in text_lines if line.strip())
        if not text:
            continue

        # Try to get sequence number from line before timestamp
        seq = i + 1
        if ts_line_idx > 0:
            seq_line = lines[ts_line_idx - 1].strip()
            if _SEQUENCE_RE.match(seq_line):
                try:
                    seq = int(seq_line)
                except ValueError:
                    pass

        segments.append(SrtSegment(
            index=seq,
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
        ))

    return segments


def validate_srt(srt_content: str) -> bool:
    """Check if content looks like valid SRT format.

    Requires at least one timestamp line in the expected format.
    """
    if not srt_content or not srt_content.strip():
        return False

    for line in srt_content.splitlines():
        if _TIMESTAMP_RE.match(line.strip()):
            return True

    return False
