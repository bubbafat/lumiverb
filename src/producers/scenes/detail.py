"""How finely a video is cut into scenes: the Detail setting (Low / Normal /
High) and everything it decides, shared by the indexer (the scheduler) and
the server (the chunk grid, the discard pass). Pure: no media libraries.

A scene's length scales with the video's:
``clamp(duration / target_scenes, min_sec, max_sec)``, then evened out so
the video is a whole number of windows (no short scrap at the end). Each
window is one scene unless a real cut (a strong change of picture) splits
it. Once the whole video is found, neighbouring scenes that look alike are
merged (``merge_runs``): a talking head is one scene, not one per window.

Thresholds are Hamming distances between 8 × 8 wavelet hashes (64 bits,
16 hex digits): each picture's light and dark at its coarsest, which a hand
or fork in front, people moving, a nudge of the camera or the light changing
leave mostly as they were. Measured on 60 of Robert's videos (Oct 28, every
camera and length; 438 neighbouring scenes, each pair judged the same scene
or not): the 16 × 16 pHash before this merged 1 of 333 pairs that were the
same scene, as its fine detail flips with anything that moves (the same
table, minutes apart: 92 to 152 of 256 bits, as unrelated pictures are);
this hash merges 46% of them at Normal, and 3% of the pairs that weren't. A
scene found before (scenes version 4) has a 16 × 16 pHash, 64 hex digits:
hamming() compares only hashes of one size, so it's never merged with one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# Lowest first: lineage records a level as its index here, and a level is
# compared as a number (made below the setting: out of date; at or above: current).
DETAILS: tuple[str, ...] = ("low", "normal", "high")
DEFAULT = "normal"


@dataclass(frozen=True)
class Detail:
    target_scenes: int  # scenes a video is cut into before merging, unless clamped
    min_sec: float  # a scene is never shorter than this (unless the video is)
    max_sec: float  # nor longer, before merging
    # A scene whose picture is within this many bits of the kept scene before
    # it is merged into it. Low merges more (looser), High keeps more (stricter):
    # of the measured pairs that were the same scene, 51% / 46% / 32% merge,
    # and 6% / 3% / 1% of those that weren't.
    same_within: int


LEVELS: dict[str, Detail] = {
    "low": Detail(target_scenes=4, min_sec=5.0, max_sec=120.0, same_within=17),
    "normal": Detail(target_scenes=8, min_sec=5.0, max_sec=60.0, same_within=14),
    "high": Detail(target_scenes=16, min_sec=3.0, max_sec=30.0, same_within=10),
}
assert tuple(LEVELS) == DETAILS

# Keyframes looked at per scene for its picture: spread across it, the sharpest kept.
SAMPLES = 5
# A real cut inside a window: a keyframe this many bits from the keyframe
# before it and from the start of its piece (above every level's
# same_within, so a cut is a stronger change than anything merged)...
CUT_BITS = 24
# ...whose picture then holds: the keyframe after it within this many bits.
# Keyframes are 2 s apart (the analysis proxy places them: its KEYFRAME_SEC),
# and a moving camera changes as much in 2 s as a cut does, so a change alone
# isn't a cut; a moving shot never holds, a shot cut to does. Kept strict: in
# footage shot in one take (Robert's, all of it measured), a real change
# within a window is rare (16 among 5,365 keyframes of the 60 videos), a
# false cut is a scene too many unless it's merged back, and a change
# missed waits for the window's end; the pHash before cut 93 times there,
# none of them real.
STEADY_BITS = 12
# ...leaving at least this long on each side (no sliver either side of a cut).
CUT_MIN_SEC = 2.0
# Videos are handed out about this long at a time, in whole windows.
CHUNK_TARGET_SEC = 30.0
HASH_SIZE = 8  # the wavelet hash's side: 8 × 8 bits
FRAME_WIDTH = 480  # keyframes are looked at this wide at most


def detail(name: str | None) -> Detail:
    return LEVELS.get(name or DEFAULT, LEVELS[DEFAULT])


def level_of(name: str | None) -> int:
    """A detail's index in DETAILS (the default's for one it doesn't know)."""
    return DETAILS.index(name) if name in DETAILS else DETAILS.index(DEFAULT)


def name_of(level: int | None) -> str:
    return DETAILS[level] if level is not None and 0 <= level < len(DETAILS) else DEFAULT


def scene_length(duration_sec: float, name: str | None) -> float:
    """The scene length the detail asks for this video, before evening out."""
    d = detail(name)
    return min(max(duration_sec / d.target_scenes, d.min_sec), d.max_sec)


def window_sec(duration_sec: float, name: str | None) -> float:
    """The scene window: the asked length evened out so the video is a whole
    number of windows. A video shorter than the shortest scene is one."""
    if duration_sec <= 0:
        return scene_length(0.0, name)
    n = max(1, round(duration_sec / scene_length(duration_sec, name)))
    return duration_sec / n


def windows_in(duration_sec: float, window: float) -> int:
    return max(1, round(duration_sec / window)) if window > 0 else 1


def chunk_grid(duration_sec: float, window: float) -> list[tuple[int, int]]:
    """The video's chunks as (start_ms, end_ms): whole windows each, about
    CHUNK_TARGET_SEC long (one window when a window is longer), so a chunk
    ends where a window does and no scene is cut short at a chunk's end."""
    if duration_sec <= 0:
        return []
    n = windows_in(duration_sec, window)
    step = duration_sec / n
    per_chunk = max(1, round(CHUNK_TARGET_SEC / step))
    out = []
    for first in range(0, n, per_chunk):
        last = min(first + per_chunk, n)
        end = duration_sec if last == n else last * step
        out.append((int(round(first * step * 1000)), int(round(end * 1000))))
    return out


def hamming(a: str | None, b: str | None) -> int | None:
    """Bits apart of two hex pHashes; None when either is missing or they're different sizes."""
    if not a or not b or len(a) != len(b):
        return None
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return None


def merge_runs(hashes: Sequence[str | None], within: int) -> list[tuple[int, int]]:
    """One left-to-right pass over a video's scenes (their pictures' pHashes,
    in order): runs of scenes to merge, as (first, last) indexes. A run goes
    on while the next scene is within `within` bits of the run's first (kept)
    scene, not just of its neighbour, so a slow drift isn't chained into one
    scene. A scene without a hash is never merged."""
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(hashes):
        j = i
        while j + 1 < len(hashes):
            d = hamming(hashes[i], hashes[j + 1])
            if d is None or d > within:
                break
            j += 1
        runs.append((i, j))
        i = j + 1
    return runs
