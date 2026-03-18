"""Fast unit tests for SceneSegmenter best-frame selection.

Covers:
- Short scene (< SKIP_FRAMES_BEST candidates): sharpest of all buffered frames is chosen.
- Long scene (>= SKIP_FRAMES_BEST candidates): first SKIP_FRAMES_BEST frames are skipped.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.video.scene_segmenter import SceneSegmenter
from src.video.video_scanner import RawFrame

_DUMMY_BYTES = b"\x00" * 12  # placeholder; sharpness/phash are patched


def _frame(pts: float) -> RawFrame:
    return RawFrame(bytes=_DUMMY_BYTES, pts=pts, width=2, height=2)


@pytest.mark.fast
def test_short_scene_chooses_sharpest_candidate():
    """3 frames; phash drift triggers at pts=3.1.
    Scene candidates are pts=0.0 (sharpness=10) and pts=1.0 (sharpness=50).
    With only 2 candidates (== SKIP_FRAMES_BEST), pool falls back to all candidates.
    The sharpest is pts=1.0, so rep_frame_ms must be 1000.
    """
    frames = [_frame(0.0), _frame(1.0), _frame(3.1)]

    sharpness_values = [10.0, 50.0, 5.0]  # index matches frame order
    sharpness_iter = iter(sharpness_values)

    with (
        patch(
            "src.video.scene_segmenter._frame_to_phash",
            side_effect=lambda _: "aabbccdd" * 8,
        ),
        patch(
            "src.video.scene_segmenter._frame_sharpness",
            side_effect=lambda _: next(sharpness_iter),
        ),
        patch(
            "src.video.scene_segmenter._hamming_hex",
            return_value=60,  # always above PHASH_THRESHOLD=51
        ),
    ):
        segmenter = SceneSegmenter(frames)
        scenes = segmenter.segment()

    assert len(scenes) >= 1
    assert scenes[0].rep_frame_ms == 1000


@pytest.mark.fast
def test_long_scene_skips_first_two_frames():
    """4 frames in a single scene (no triggers).
    Sharpness: [10, 20, 50, 30] at pts [0.0, 1.0, 2.0, 3.0].
    Pool starts at index 2 (SKIP_FRAMES_BEST=2): candidates are pts=2.0 (50) and pts=3.0 (30).
    Best is pts=2.0, so rep_frame_ms must be 2000.
    """
    frames = [_frame(0.0), _frame(1.0), _frame(2.0), _frame(3.0)]

    sharpness_values = [10.0, 20.0, 50.0, 30.0]
    sharpness_iter = iter(sharpness_values)

    with (
        patch(
            "src.video.scene_segmenter._frame_to_phash",
            side_effect=lambda _: "aabbccdd" * 8,
        ),
        patch(
            "src.video.scene_segmenter._frame_sharpness",
            side_effect=lambda _: next(sharpness_iter),
        ),
        patch(
            "src.video.scene_segmenter._hamming_hex",
            return_value=0,  # always below PHASH_THRESHOLD; no phash trigger
        ),
    ):
        segmenter = SceneSegmenter(frames)
        scenes = segmenter.segment()

    assert len(scenes) == 1
    assert scenes[0].rep_frame_ms == 2000
