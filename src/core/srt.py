"""SRT subtitle format parsing utilities."""

from __future__ import annotations

import re

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
