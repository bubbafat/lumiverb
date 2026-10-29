"""A chunk's scenes, from its keyframes (src/producers/scenes/detail.py).

A chunk is a whole number of scene windows. Each window is one scene unless
a real cut splits it: a keyframe far from the one before it and from the
start of its piece (CUT_BITS) whose picture then holds (the next keyframe
within STEADY_BITS of it), with CUT_MIN_SEC left on both sides. Each scene's picture is the sharpest of
up to SAMPLES keyframes spread across it, and its hash is that picture's
(what the discard pass compares, once the whole video is found): a wavelet
hash, HASH_SIZE × HASH_SIZE bits (detail.py says why), kept as `phash`, the
picture's perceptual hash.

Frames are taken in video time (absolute pts); those outside the chunk are
left out, so an overlapping scan changes nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

import cv2
import imagehash
import numpy as np
from PIL import Image

from src.processing.video.video_scanner import RawFrame
from src.producers.scenes.detail import (
    CUT_BITS,
    CUT_MIN_SEC,
    HASH_SIZE,
    SAMPLES,
    STEADY_BITS,
    hamming,
    windows_in,
)

_log = logging.getLogger(__name__)

# Why a scene ends where it does.
WINDOW = "window"  # its window's end (or the chunk's)
CUT = "cut"  # a real cut


@dataclass
class Scene:
    """One scene: its span, its picture and that picture's hash."""

    start_ms: int
    end_ms: int
    rep_frame_ms: int
    sharpness_score: float | None
    keep_reason: str | None
    phash: str | None


def _frame_to_phash(raw: RawFrame, hash_size: int = HASH_SIZE) -> str | None:
    """A raw RGB frame's wavelet hash, as hex; None when it can't be made."""
    try:
        arr = np.frombuffer(raw.bytes, dtype=np.uint8).reshape((raw.height, raw.width, 3))
        return str(imagehash.whash(Image.fromarray(arr, mode="RGB"), hash_size=hash_size))
    except Exception as e:  # noqa: BLE001 — a frame without a hash is never a cut, never merged
        _log.debug("phash failed for frame at %.2f: %s", raw.pts, e)
        return None


def _frame_sharpness(raw: RawFrame) -> float:
    """Laplacian variance (higher = sharper)."""
    arr = np.frombuffer(raw.bytes, dtype=np.uint8).reshape((raw.height, raw.width, 3))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F, ksize=3).var())


def spread(n: int, k: int = SAMPLES) -> list[int]:
    """Up to k indexes of n, spread evenly (the middle of each of k equal parts): all when n ≤ k."""
    if n <= k:
        return list(range(n))
    return sorted({int((i + 0.5) * n / k) for i in range(k)})


class SceneSegmenter:
    """Cuts one chunk [start, end) into scenes, in windows of about window_sec.
    last: the video's last chunk, which takes a frame at its very end too."""

    def __init__(self, frames: Iterable[RawFrame], *, start: float, end: float, window_sec: float,
                 last: bool = False) -> None:
        self._frames = frames
        self._start, self._end = start, end
        self._n = windows_in(end - start, window_sec)
        self._last = last

    def _boundaries(self) -> list[float]:
        step = (self._end - self._start) / self._n
        return [self._start + i * step for i in range(self._n)] + [self._end]

    @staticmethod
    def _cut(hashes: list[str | None], anchor: str | None, m: int) -> bool:
        """A real cut at keyframe m: far from the keyframe before it and from
        its piece's start, and the keyframe after it alike (the picture holds)."""
        if m == 0 or m + 1 >= len(hashes):
            return False
        before, start, after = (hamming(hashes[m - 1], hashes[m]), hamming(anchor, hashes[m]),
                                hamming(hashes[m], hashes[m + 1]))
        return (None not in (before, start, after) and before > CUT_BITS and start > CUT_BITS
                and after <= STEADY_BITS)

    def segment(self) -> list[Scene]:
        frames = [f for f in self._frames
                  if self._start <= f.pts < self._end or (self._last and f.pts == self._end)]
        frames.sort(key=lambda f: f.pts)
        hashes = [_frame_to_phash(f) for f in frames]
        bounds = self._boundaries()

        # Pieces: (start, end, frame indexes, why it ends). A window without a
        # keyframe joins the piece after it (or the one before, at the chunk's end).
        pieces: list[tuple[float, float, list[int], str]] = []
        carry: float | None = None  # where the next piece starts, when windows before it were empty
        i = 0
        for w in range(self._n):
            ws, we = bounds[w], bounds[w + 1]
            members: list[int] = []
            while i < len(frames) and (frames[i].pts < we or (w == self._n - 1)):
                members.append(i)
                i += 1
            if not members:
                carry = ws if carry is None else carry
                continue
            piece_start = carry if carry is not None else ws
            carry = None
            anchor = hashes[members[0]]
            current: list[int] = []
            for m in members:
                pts = frames[m].pts
                if (current and pts - piece_start >= CUT_MIN_SEC and we - pts >= CUT_MIN_SEC
                        and self._cut(hashes, anchor, m)):
                    pieces.append((piece_start, pts, current, CUT))
                    piece_start, anchor, current = pts, hashes[m], []
                current.append(m)
            pieces.append((piece_start, we, current, WINDOW))
        if carry is not None and pieces:  # empty windows at the chunk's end: the last scene runs on
            s, _, members, why = pieces[-1]
            pieces[-1] = (s, self._end, members, why)

        scenes = []
        for start, end, members, why in pieces:
            sampled = [members[k] for k in spread(len(members))]
            sharpness = {m: _frame_sharpness(frames[m]) for m in sampled}
            best = max(sampled, key=lambda m: sharpness[m])
            scenes.append(Scene(
                start_ms=int(round(start * 1000)), end_ms=int(round(end * 1000)),
                rep_frame_ms=int(round(frames[best].pts * 1000)), sharpness_score=sharpness[best],
                keep_reason=why, phash=hashes[best]))
        return scenes
