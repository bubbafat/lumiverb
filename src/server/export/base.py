"""Shared types and timing math for editor exports."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol
from urllib.parse import quote

# Used when a clip hasn't been probed. Editors read the file's real rate
# on import; the XML only needs a consistent one.
FALLBACK_RATE = (30, 1)

# Rates editors expect. A variable-frame-rate phone clip can average 29.92
# fps; within 1% of a standard rate, the standard one is used.
STANDARD_RATES = [
    Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
    Fraction(48000, 1001), Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60),
    Fraction(100), Fraction(120000, 1001), Fraction(120),
]
SNAP_TOLERANCE = 0.01

TIMECODE = re.compile(r"^\d{2}:[0-5]\d:[0-5]\d[:;]\d{2}$")


@dataclass(frozen=True)
class ExportClip:
    asset_id: str
    name: str  # file name shown in the editor
    path: str  # absolute path of the original on the editing machine
    duration_sec: float | None
    frame_rate_num: int | None
    frame_rate_den: int | None
    width: int | None
    height: int | None
    start_timecode: str | None
    drop_frame: bool | None
    audio_channels: int | None
    audio_sample_rate: int | None

    @property
    def rate(self) -> tuple[int, int]:
        """(num, den) frames per second, snapped to a standard rate when close."""
        if not (self.frame_rate_num and self.frame_rate_den):
            return FALLBACK_RATE
        raw = Fraction(self.frame_rate_num, self.frame_rate_den)
        nearest = min(STANDARD_RATES, key=lambda std: abs(raw - std))
        if abs(raw - nearest) / nearest <= SNAP_TOLERANCE:
            raw = nearest
        return raw.numerator, raw.denominator

    @property
    def timebase(self) -> int:
        """Integer frames per second used for counting (30 for 29.97)."""
        num, den = self.rate
        return round(num / den)

    @property
    def ntsc(self) -> bool:
        return self.rate[1] == 1001

    @property
    def duration_frames(self) -> int:
        num, den = self.rate
        return round((self.duration_sec or 0.0) * num / den)

    @property
    def timecode(self) -> str | None:
        """The start timecode if it's well formed; a bad one never breaks an export."""
        tc = self.start_timecode
        return tc if tc and TIMECODE.match(tc) else None

    @property
    def is_drop_frame(self) -> bool:
        return bool(self.timecode and self.drop_frame)

    @property
    def start_frames(self) -> int:
        if not self.timecode:
            return 0
        return timecode_to_frames(self.timecode, self.timebase, self.is_drop_frame)

    @property
    def file_url(self) -> str:
        """file:///… with the path percent-encoded."""
        return "file://" + quote(self.path)


@dataclass(frozen=True)
class ExportBin:
    name: str
    clips: list[ExportClip]


class ExportProvider(Protocol):
    id: str
    label: str  # shown in the format chooser
    file_extension: str
    content_type: str

    def render(self, bin_: ExportBin) -> bytes: ...


def timecode_to_frames(tc: str, timebase: int, drop_frame: bool) -> int:
    """Frame count of an HH:MM:SS:FF (or ;FF) timecode.

    Drop-frame skips frame numbers 0 and 1 (0-3 at 60) at the start of
    every minute except each tenth minute.
    """
    parts = tc.replace(";", ":").replace(".", ":").split(":")
    h, m, s, f = (int(p) for p in parts)
    frames = ((h * 60 + m) * 60 + s) * timebase + f
    if drop_frame:
        dropped_per_minute = round(timebase / 15)  # 2 at 30, 4 at 60
        minutes = h * 60 + m
        frames -= dropped_per_minute * (minutes - minutes // 10)
    return frames


def timeline_lead(clips: list[ExportClip]) -> ExportClip:
    """The clip whose rate and size the timeline takes: the first one with
    a known frame size (an unprobed clip has none), else the first."""
    return next((c for c in clips if c.width and c.height), clips[0])


def timeline_frames(clip: ExportClip, lead: ExportClip) -> int:
    """The clip's length on the lead's frame grid. At another rate, round
    down, so the timeline never runs past the end of the clip's media."""
    if clip.rate == lead.rate:
        return clip.duration_frames
    num, den = lead.rate
    return math.floor((clip.duration_sec or 0.0) * num / den)
