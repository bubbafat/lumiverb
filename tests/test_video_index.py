"""Unit tests for video scene detection orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.cli.video_index import index_video_scenes


class _FakeResponse:
    """Minimal response mock."""

    def __init__(self, status_code: int = 200, data: dict | None = None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _FakeScene:
    def __init__(self, start_ms, end_ms, rep_frame_ms, sharpness_score=100.0, keep_reason="temporal", phash="abc"):
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.rep_frame_ms = rep_frame_ms
        self.sharpness_score = sharpness_score
        self.keep_reason = keep_reason
        self.phash = phash


class _FakeSegmenter:
    def __init__(self, scenes: list[_FakeScene]):
        self._scenes = scenes
        self.next_anchor_phash = "deadbeef"
        self.next_scene_start_ms = None

    def segment(self):
        return self._scenes


@patch("src.cli.video_index.VideoScanner")
@patch("src.cli.video_index.SceneSegmenter")
def test_index_video_scenes_single_chunk(mock_segmenter_cls, mock_scanner_cls):
    """Single chunk with two scenes completes successfully."""
    client = MagicMock()

    # Init chunks
    client.post.return_value = _FakeResponse(data={"chunk_count": 1, "already_initialized": False})

    # Claim chunk, then 204 (done)
    work_order = _FakeResponse(200, {
        "chunk_id": "chunk_1",
        "worker_id": "vid_abc",
        "chunk_index": 0,
        "start_ts": 0.0,
        "end_ts": 30.0,
        "overlap_sec": 2.0,
        "anchor_phash": None,
        "scene_start_ts": None,
    })
    done_resp = _FakeResponse(204)
    client.raw.side_effect = [work_order, done_resp]

    # Scanner returns empty iterator (segmenter controls scenes)
    mock_scanner_cls.return_value.scan.return_value = iter([])

    # Segmenter returns 2 scenes
    scenes = [
        _FakeScene(0, 15000, 7000),
        _FakeScene(15000, 30000, 22000),
    ]
    segmenter = _FakeSegmenter(scenes)
    mock_segmenter_cls.return_value = segmenter

    result = index_video_scenes(
        client=client,
        source_path=Path("/fake/video.mp4"),
        asset_id="asset_1",
        duration_sec=30.0,
        rel_path="video.mp4",
    )

    assert result["scenes"] == 2
    assert result["chunks"] == 1
    assert result["elapsed"] > 0

    # Verify chunk init was called
    client.post.assert_any_call(
        "/v1/video/asset_1/chunks",
        json={"duration_sec": 30.0},
    )

    # Verify chunk complete was called with correct scene data
    complete_call = [c for c in client.post.call_args_list if "complete" in str(c)]
    assert len(complete_call) == 1
    body = complete_call[0].kwargs["json"]
    assert len(body["scenes"]) == 2
    assert body["next_anchor_phash"] == "deadbeef"


@patch("src.cli.video_index.VideoScanner")
@patch("src.cli.video_index.SceneSegmenter")
def test_index_video_scenes_all_complete(mock_segmenter_cls, mock_scanner_cls):
    """When all chunks are already complete, returns 0 scenes."""
    client = MagicMock()
    client.post.return_value = _FakeResponse(data={"chunk_count": 1, "already_initialized": True})

    # Immediate 204 — all done
    client.raw.return_value = _FakeResponse(204)

    result = index_video_scenes(
        client=client,
        source_path=Path("/fake/video.mp4"),
        asset_id="asset_1",
        duration_sec=30.0,
        rel_path="video.mp4",
    )

    assert result["scenes"] == 0
    assert result["chunks"] == 0


@patch("src.cli.video_index.VideoScanner")
@patch("src.cli.video_index.SceneSegmenter")
def test_index_video_scenes_chunk_failure(mock_segmenter_cls, mock_scanner_cls):
    """Failed chunks are reported to server and processing continues."""
    from src.video.video_scanner import SyncError

    client = MagicMock()
    client.post.return_value = _FakeResponse(data={"chunk_count": 1, "already_initialized": False})

    work_order = _FakeResponse(200, {
        "chunk_id": "chunk_1",
        "worker_id": "vid_abc",
        "chunk_index": 0,
        "start_ts": 0.0,
        "end_ts": 30.0,
        "overlap_sec": 2.0,
    })
    done_resp = _FakeResponse(204)
    client.raw.side_effect = [work_order, done_resp]

    # Scanner raises SyncError
    mock_scanner_cls.return_value.scan.side_effect = SyncError("FFmpeg hung")

    result = index_video_scenes(
        client=client,
        source_path=Path("/fake/video.mp4"),
        asset_id="asset_1",
        duration_sec=30.0,
        rel_path="video.mp4",
    )

    assert result["scenes"] == 0
    assert result["chunks"] == 0

    # Verify fail was posted
    fail_calls = [c for c in client.post.call_args_list if "fail" in str(c)]
    assert len(fail_calls) == 1
    fail_body = fail_calls[0].kwargs["json"]
    assert fail_body["worker_id"] == "vid_abc"
    assert "FFmpeg hung" in fail_body["error_message"]
