"""Scene segmentation from raw frames using pHash drift and temporal ceiling.

See docs/reference/video_scene_segmentation.md for constants and trigger strategy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

import cv2
import imagehash
import numpy as np
from PIL import Image

from src.client.video.video_scanner import RawFrame

_log = logging.getLogger(__name__)

PHASH_THRESHOLD = 51
PHASH_HASH_SIZE = 16
TEMPORAL_CEILING_SEC = 30.0
DEBOUNCE_SEC = 3.0
SKIP_FRAMES_BEST = 2  # skip first N frames of a scene when picking the sharpest representative


class SceneKeepReason(str, Enum):
    temporal = "temporal"
    phash = "phash"
    forced = "forced"


@dataclass
class Scene:
    """One detected scene with representative frame and metadata."""

    start_ms: int
    end_ms: int
    rep_frame_ms: int
    sharpness_score: float | None
    keep_reason: str | None
    phash: str | None


def _frame_to_phash(raw: RawFrame) -> str | None:
    """Compute perceptual hash from raw RGB frame. Returns hex string or None."""
    try:
        arr = np.frombuffer(raw.bytes, dtype=np.uint8).reshape(
            (raw.height, raw.width, 3)
        )
        pil = Image.fromarray(arr, mode="RGB")
        h = imagehash.phash(pil, hash_size=PHASH_HASH_SIZE)
        return str(h)
    except Exception as e:
        _log.debug("phash failed for frame at %.2f: %s", raw.pts, e)
        return None


def _frame_sharpness(raw: RawFrame) -> float:
    """Laplacian variance for sharpness (higher = sharper)."""
    arr = np.frombuffer(raw.bytes, dtype=np.uint8).reshape(
        (raw.height, raw.width, 3)
    )
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F, ksize=3).var())


def _select_best(candidates: list[tuple[float, float]]) -> tuple[float, float]:
    """Select (pts, sharpness) from scene candidates, skipping first SKIP_FRAMES_BEST if possible."""
    if not candidates:
        return 0.0, -1.0
    pool = candidates[SKIP_FRAMES_BEST:] if len(candidates) > SKIP_FRAMES_BEST else candidates
    return max(pool, key=lambda c: c[1])


def _hamming_hex(a: str, b: str) -> int:
    """Hamming distance between two hex phash strings."""
    if not a or not b or len(a) != len(b):
        return 256
    ha = imagehash.hex_to_hash(a)
    hb = imagehash.hex_to_hash(b)
    return ha - hb


class SceneSegmenter:
    """
    Segments a list of raw frames into scenes using pHash drift and temporal ceiling.
    Tracks best (sharpest) frame per scene; exposes next_anchor_phash and next_scene_start_ms
    after segment() for anchor propagation to the next chunk.
    """

    def __init__(
        self,
        frames: Iterable[RawFrame],
        anchor_phash: str | None = None,
        scene_start_ts: float | None = None,
    ) -> None:
        self._frames = frames
        self._anchor_phash = anchor_phash
        self._scene_start_ts = scene_start_ts
        self.next_anchor_phash: str | None = None
        self.next_scene_start_ms: int | None = None

    def segment(self) -> list[Scene]:
        """
        Run segmentation. After return, self.next_anchor_phash and self.next_scene_start_ms
        are set for the next chunk's anchor state.
        """
        scenes: list[Scene] = []
        anchor_phash = self._anchor_phash
        scene_start_pts = self._scene_start_ts
        scene_candidates: list[tuple[float, float]] = []  # (pts, sharpness) for current scene
        last_raw: RawFrame | None = None
        had_frames = False

        for raw in self._frames:
            had_frames = True
            last_raw = raw
            pts = raw.pts
            sharpness = _frame_sharpness(raw)

            if scene_start_pts is None:
                scene_start_pts = pts
                anchor_phash = _frame_to_phash(raw)
                scene_candidates = [(pts, sharpness)]
                continue

            elapsed = pts - scene_start_pts
            frame_phash = _frame_to_phash(raw)

            trigger: SceneKeepReason | None = None
            if elapsed >= TEMPORAL_CEILING_SEC:
                trigger = SceneKeepReason.temporal
            elif (
                anchor_phash is not None
                and frame_phash is not None
                and elapsed >= DEBOUNCE_SEC
            ):
                if _hamming_hex(anchor_phash, frame_phash) > PHASH_THRESHOLD:
                    trigger = SceneKeepReason.phash

            if trigger is not None:
                # Close current scene using buffered candidates (trigger frame excluded)
                best_pts, best_sharpness = _select_best(scene_candidates)
                rep_ms = int(round(best_pts * 1000))
                start_ms = int(round((scene_start_pts or 0) * 1000))
                end_ms = int(round(pts * 1000))
                anchor_for_next = frame_phash if frame_phash else anchor_phash
                scenes.append(
                    Scene(
                        start_ms=start_ms,
                        end_ms=end_ms,
                        rep_frame_ms=rep_ms,
                        sharpness_score=best_sharpness if best_sharpness >= 0 else None,
                        keep_reason=trigger.value,
                        phash=anchor_for_next,
                    )
                )
                # Start new scene with trigger frame
                scene_start_pts = pts
                anchor_phash = frame_phash
                scene_candidates = [(pts, sharpness)]
                continue

            # In-scene: buffer this frame as a candidate
            scene_candidates.append((pts, sharpness))

        if not had_frames:
            return []

        # Close final scene (forced)
        if scene_start_pts is not None:
            best_pts, best_sharpness = _select_best(scene_candidates)
            rep_ms = int(round(best_pts * 1000))
            start_ms = int(round(scene_start_pts * 1000))
            end_pts = last_raw.pts if last_raw else scene_start_pts
            end_ms = int(round(end_pts * 1000))
            last_phash = _frame_to_phash(last_raw) if last_raw else None
            scenes.append(
                Scene(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    rep_frame_ms=rep_ms,
                    sharpness_score=best_sharpness if best_sharpness >= 0 else None,
                    keep_reason=SceneKeepReason.forced.value,
                    phash=last_phash or anchor_phash,
                )
            )
            self.next_anchor_phash = last_phash or anchor_phash
            self.next_scene_start_ms = None
        else:
            self.next_anchor_phash = anchor_phash
            self.next_scene_start_ms = None

        return scenes
