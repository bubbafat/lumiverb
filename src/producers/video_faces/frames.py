"""Which frames of a video are looked at for faces: the Detail setting (Low /
Normal / High). Pure: no media libraries.

Low: each scene's picture (the sharpest of its keyframes, chosen when its
scenes were found: src/producers/scenes). Normal: three a scene, its
picture and a quarter and three quarters of the way through it. High: the
scenes' pictures and a frame every EVERY_SEC across the video.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence

# Lowest first: lineage records a level as its index here (made below the
# setting: out of date; at or above: current).
DETAILS: tuple[str, ...] = ("low", "normal", "high")
DEFAULT = "low"

EVERY_SEC = 3.0  # High
# Frames closer than this are one picture: the later is left out.
APART_MS = 500


def frame_times(scenes: Sequence[dict], duration_ms: int, detail: str | None) -> list[int]:
    """The times (ms) of the frames to look at, in order. scenes: the video's,
    each {start_ms, end_ms, rep_frame_ms}. A video without scenes is looked at
    as one scene, its picture in the middle."""
    detail = detail if detail in DETAILS else DEFAULT
    if not scenes:
        scenes = [{"start_ms": 0, "end_ms": duration_ms, "rep_frame_ms": duration_ms // 2}]
    picked = [int(s["rep_frame_ms"]) for s in scenes]
    if detail == "normal":
        for s in scenes:
            start, end = int(s["start_ms"]), int(s["end_ms"])
            picked += [start + (end - start) // 4, start + 3 * (end - start) // 4]
    elif detail == "high":
        step = int(EVERY_SEC * 1000)
        picked += list(range(step // 2, max(duration_ms, 1), step))
    last = max(duration_ms - 1, 0)
    kept: list[int] = []
    # The scenes' pictures first: a time near one is the same picture.
    for t in picked:
        t = min(max(t, 0), last) if duration_ms > 0 else max(t, 0)
        i = bisect.bisect_left(kept, t)
        if (i == len(kept) or kept[i] - t >= APART_MS) and (i == 0 or t - kept[i - 1] >= APART_MS):
            kept.insert(i, t)
    return kept
