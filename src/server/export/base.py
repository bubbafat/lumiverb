"""Shared types and timing math for editor exports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

# Used when a clip hasn't been probed. Editors read the file's real rate
# on import; the XML only needs a consistent one.
FALLBACK_RATE = (30, 1)


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
        """(num, den) frames per second."""
        if self.frame_rate_num and self.frame_rate_den:
            return self.frame_rate_num, self.frame_rate_den
        return FALLBACK_RATE

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
    def start_frames(self) -> int:
        if not self.start_timecode:
            return 0
        return timecode_to_frames(self.start_timecode, self.timebase, bool(self.drop_frame))

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
