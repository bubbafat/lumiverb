"""Unit tests for video scene detection and enrichment orchestration."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.client.cli.video_index import index_video_scenes, run_video_index, enrich_video_scenes, run_video_enrich


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


@patch("src.client.cli.video_index.VideoScanner")
@patch("src.client.cli.video_index.SceneSegmenter")
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


@patch("src.client.cli.video_index.VideoScanner")
@patch("src.client.cli.video_index.SceneSegmenter")
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


@patch("src.client.cli.video_index.VideoScanner")
@patch("src.client.cli.video_index.SceneSegmenter")
def test_index_video_scenes_chunk_failure(mock_segmenter_cls, mock_scanner_cls):
    """Failed chunks are reported to server and processing continues."""
    from src.client.video.video_scanner import SyncError

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


@patch("src.client.cli.video_index.VideoScanner")
@patch("src.client.cli.video_index.SceneSegmenter")
def test_index_video_scenes_multi_chunk(mock_segmenter_cls, mock_scanner_cls):
    """Two chunks: scene_index is cumulative across chunks."""
    client = MagicMock()
    client.post.return_value = _FakeResponse(data={"chunk_count": 2, "already_initialized": False})

    chunk0 = _FakeResponse(200, {
        "chunk_id": "chunk_0", "worker_id": "vid_w", "chunk_index": 0,
        "start_ts": 0.0, "end_ts": 30.0, "overlap_sec": 2.0,
        "anchor_phash": None, "scene_start_ts": None,
    })
    chunk1 = _FakeResponse(200, {
        "chunk_id": "chunk_1", "worker_id": "vid_w", "chunk_index": 1,
        "start_ts": 30.0, "end_ts": 60.0, "overlap_sec": 2.0,
        "anchor_phash": "prev_hash", "scene_start_ts": None,
    })
    done_resp = _FakeResponse(204)
    client.raw.side_effect = [chunk0, chunk1, done_resp]

    mock_scanner_cls.return_value.scan.return_value = iter([])

    # Each chunk produces 1 scene
    call_count = [0]
    def make_segmenter(*args, **kwargs):
        call_count[0] += 1
        scenes = [_FakeScene(0, 30000, 15000, phash=f"hash_{call_count[0]}")]
        return _FakeSegmenter(scenes)

    mock_segmenter_cls.side_effect = make_segmenter

    result = index_video_scenes(
        client=client,
        source_path=Path("/fake/video.mp4"),
        asset_id="asset_1",
        duration_sec=60.0,
        rel_path="video.mp4",
    )

    assert result["scenes"] == 2
    assert result["chunks"] == 2

    # Verify both completes were called with correct chunk_ids
    complete_calls = [c for c in client.post.call_args_list if "complete" in str(c)]
    assert len(complete_calls) == 2
    # First chunk's scene_index starts at 0
    assert complete_calls[0].kwargs["json"]["scenes"][0]["scene_index"] == 0
    # Second chunk's scene_index starts at 1 (cumulative)
    assert complete_calls[1].kwargs["json"]["scenes"][0]["scene_index"] == 1


@patch("src.client.cli.video_index.VideoScanner")
@patch("src.client.cli.video_index.SceneSegmenter")
def test_index_video_scenes_overlap_calculation(mock_segmenter_cls, mock_scanner_cls):
    """Scanner is called with start_ts - overlap for anchor continuity."""
    client = MagicMock()
    client.post.return_value = _FakeResponse(data={"chunk_count": 1, "already_initialized": False})

    work = _FakeResponse(200, {
        "chunk_id": "c1", "worker_id": "w1", "chunk_index": 1,
        "start_ts": 30.0, "end_ts": 60.0, "overlap_sec": 2.0,
        "anchor_phash": "abc", "scene_start_ts": None,
    })
    client.raw.side_effect = [work, _FakeResponse(204)]
    mock_scanner_cls.return_value.scan.return_value = iter([])
    mock_segmenter_cls.return_value = _FakeSegmenter([])

    index_video_scenes(
        client=client,
        source_path=Path("/fake/video.mp4"),
        asset_id="asset_1",
        duration_sec=60.0,
        rel_path="video.mp4",
    )

    # Scanner should be called with start_ts=28.0 (30.0 - 2.0), end_ts=60.0
    mock_scanner_cls.return_value.scan.assert_called_once_with(28.0, 60.0)

    # Segmenter should receive the anchor_phash from work order
    mock_segmenter_cls.assert_called_once()
    _, kwargs = mock_segmenter_cls.call_args
    assert kwargs.get("anchor_phash") == "abc"


@patch("src.client.cli.video_index.VideoScanner")
@patch("src.client.cli.video_index.SceneSegmenter")
def test_index_video_scenes_scene_fields_complete(mock_segmenter_cls, mock_scanner_cls):
    """All scene fields are passed through to the server."""
    client = MagicMock()
    client.post.return_value = _FakeResponse(data={"chunk_count": 1, "already_initialized": False})

    work = _FakeResponse(200, {
        "chunk_id": "c1", "worker_id": "w1", "chunk_index": 0,
        "start_ts": 0.0, "end_ts": 30.0, "overlap_sec": 2.0,
    })
    client.raw.side_effect = [work, _FakeResponse(204)]
    mock_scanner_cls.return_value.scan.return_value = iter([])

    scene = _FakeScene(
        start_ms=5000, end_ms=25000, rep_frame_ms=12000,
        sharpness_score=42.5, keep_reason="phash", phash="deadbeef",
    )
    mock_segmenter_cls.return_value = _FakeSegmenter([scene])

    index_video_scenes(
        client=client,
        source_path=Path("/fake/video.mp4"),
        asset_id="asset_1",
        duration_sec=30.0,
        rel_path="video.mp4",
    )

    complete_call = [c for c in client.post.call_args_list if "complete" in str(c)]
    result_scene = complete_call[0].kwargs["json"]["scenes"][0]
    assert result_scene["start_ms"] == 5000
    assert result_scene["end_ms"] == 25000
    assert result_scene["rep_frame_ms"] == 12000
    assert result_scene["sharpness_score"] == 42.5
    assert result_scene["keep_reason"] == "phash"
    assert result_scene["phash"] == "deadbeef"
    assert result_scene["scene_index"] == 0


@patch("src.client.cli.video_index.index_video_scenes")
def test_run_video_index_missing_source(mock_index):
    """Videos with missing source files are skipped with fail count."""
    progress = MagicMock()
    progress.console = MagicMock()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # Don't create the video file — it should be missing

        run_video_index(
            client=MagicMock(),
            root_path=root,
            videos=[{"asset_id": "a1", "rel_path": "missing.mp4", "duration_sec": 10.0}],
            console=MagicMock(),
            progress=progress,
            task_id=0,
        )

    # index_video_scenes should NOT have been called
    mock_index.assert_not_called()
    # Progress should show 1 advance with fail=1
    progress.advance.assert_called_once_with(0, 1)
    progress.update.assert_called_once_with(0, ok=0, fail=1)


@patch("src.client.cli.video_index.index_video_scenes")
def test_run_video_index_happy_path(mock_index):
    """Processes two videos, updates progress correctly."""
    mock_index.return_value = {"scenes": 3, "chunks": 1, "elapsed": 1.5}
    progress = MagicMock()
    progress.console = MagicMock()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "a.mp4").write_bytes(b"\x00" * 100)
        (root / "b.mp4").write_bytes(b"\x00" * 100)

        run_video_index(
            client=MagicMock(),
            root_path=root,
            videos=[
                {"asset_id": "a1", "rel_path": "a.mp4", "duration_sec": 30.0},
                {"asset_id": "a2", "rel_path": "b.mp4", "duration_sec": 60.0},
            ],
            console=MagicMock(),
            progress=progress,
            task_id=0,
        )

    assert mock_index.call_count == 2
    assert progress.advance.call_count == 2
    # Final update should show ok=2, fail=0
    last_update = progress.update.call_args_list[-1]
    assert last_update.kwargs["ok"] == 2
    assert last_update.kwargs["fail"] == 0


# ---------------------------------------------------------------------------
# Scene enrichment tests
# ---------------------------------------------------------------------------


class _FakeFFmpegAttempt:
    def __init__(self, ok=True):
        self.ok = ok
        self.stderr = ""
    def stderr_tail(self):
        return ""


def _mock_extract_ok(source, dest, timestamp=0.0):
    """Mock that writes fake JPEG bytes so the file size check passes."""
    dest.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    return _FakeFFmpegAttempt(ok=True)


@patch("src.client.cli.video_index.extract_video_frame_detailed")
def test_enrich_video_scenes_with_vision(mock_extract):
    """Enriches scenes: extracts rep frame, calls vision, patches + syncs."""
    mock_extract.side_effect = _mock_extract_ok

    client = MagicMock()
    # GET /v1/video/{asset_id}/scenes
    client.get.return_value = _FakeResponse(data={
        "scenes": [
            {"scene_id": "scn_1", "rep_frame_ms": 5000, "description": None},
            {"scene_id": "scn_2", "rep_frame_ms": 20000, "description": "already done"},
        ]
    })
    client.post.return_value = _FakeResponse()
    client.patch.return_value = _FakeResponse()

    vision_provider = MagicMock()
    vision_provider.describe.return_value = {"description": "A sunset", "tags": ["sunset", "sky"]}

    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir) / "video.mp4"
        source.write_bytes(b"\x00" * 100)

        result = enrich_video_scenes(
            client=client,
            source_path=source,
            asset_id="asset_1",
            rel_path="video.mp4",
            vision_provider=vision_provider,
            vision_model_id="test-model",
        )

    assert result["enriched"] == 1
    assert result["skipped"] == 1  # scn_2 already has description
    assert result["failed"] == 0

    # Vision was called once (only for scn_1)
    vision_provider.describe.assert_called_once()

    # PATCH was called for scn_1
    patch_calls = [c for c in client.patch.call_args_list if "scenes" in str(c)]
    assert len(patch_calls) == 1
    body = patch_calls[0].kwargs["json"]
    assert body["description"] == "A sunset"
    assert body["tags"] == ["sunset", "sky"]
    assert body["model_id"] == "test-model"

    # Sync was called for scn_1
    sync_calls = [c for c in client.post.call_args_list if "sync" in str(c)]
    assert len(sync_calls) == 1


@patch("src.client.cli.video_index.extract_video_frame_detailed")
def test_enrich_video_scenes_without_vision(mock_extract):
    """Without vision provider, extracts rep frames but skips vision/sync."""
    mock_extract.side_effect = _mock_extract_ok

    client = MagicMock()
    client.get.return_value = _FakeResponse(data={
        "scenes": [{"scene_id": "scn_1", "rep_frame_ms": 5000, "description": None}]
    })
    client.post.return_value = _FakeResponse()

    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir) / "video.mp4"
        source.write_bytes(b"\x00" * 100)

        result = enrich_video_scenes(
            client=client,
            source_path=source,
            asset_id="asset_1",
            rel_path="video.mp4",
            vision_provider=None,
            vision_model_id=None,
        )

    assert result["enriched"] == 1
    assert result["failed"] == 0

    # Artifact upload was called
    upload_calls = [c for c in client.post.call_args_list if "scene_rep" in str(c)]
    assert len(upload_calls) == 1

    # No PATCH or sync (no vision provider)
    assert client.patch.call_count == 0
    sync_calls = [c for c in client.post.call_args_list if "sync" in str(c)]
    assert len(sync_calls) == 0


@patch("src.client.cli.video_index.extract_video_frame_detailed")
def test_enrich_video_scenes_extraction_failure(mock_extract):
    """Failed rep frame extraction counts as failure, continues to next scene."""
    mock_extract.return_value = _FakeFFmpegAttempt(ok=False)

    client = MagicMock()
    client.get.return_value = _FakeResponse(data={
        "scenes": [
            {"scene_id": "scn_1", "rep_frame_ms": 5000, "description": None},
            {"scene_id": "scn_2", "rep_frame_ms": 20000, "description": None},
        ]
    })

    with tempfile.TemporaryDirectory() as tmpdir:
        source = Path(tmpdir) / "video.mp4"
        source.write_bytes(b"\x00" * 100)

        result = enrich_video_scenes(
            client=client,
            source_path=source,
            asset_id="asset_1",
            rel_path="video.mp4",
            vision_provider=MagicMock(),
            vision_model_id="test-model",
        )

    assert result["enriched"] == 0
    assert result["failed"] == 2


@patch("src.client.cli.video_index.enrich_video_scenes")
def test_run_video_enrich_happy_path(mock_enrich):
    """Processes videos and updates progress."""
    mock_enrich.return_value = {"enriched": 2, "skipped": 0, "failed": 0, "elapsed": 1.0}
    progress = MagicMock()
    progress.console = MagicMock()

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "a.mp4").write_bytes(b"\x00" * 100)

        run_video_enrich(
            client=MagicMock(),
            root_path=root,
            videos=[{"asset_id": "a1", "rel_path": "a.mp4"}],
            vision_provider=MagicMock(),
            vision_model_id="test-model",
            console=MagicMock(),
            progress=progress,
            task_id=0,
        )

    mock_enrich.assert_called_once()
    progress.advance.assert_called_once_with(0, 1)
    last_update = progress.update.call_args_list[-1]
    assert last_update.kwargs["ok"] == 1
    assert last_update.kwargs["fail"] == 0


@patch("src.client.cli.video_index.enrich_video_scenes")
def test_run_video_enrich_missing_source(mock_enrich):
    """Missing source files are skipped."""
    progress = MagicMock()
    progress.console = MagicMock()

    with tempfile.TemporaryDirectory() as tmpdir:
        run_video_enrich(
            client=MagicMock(),
            root_path=Path(tmpdir),
            videos=[{"asset_id": "a1", "rel_path": "missing.mp4"}],
            vision_provider=MagicMock(),
            vision_model_id="test-model",
            console=MagicMock(),
            progress=progress,
            task_id=0,
        )

    mock_enrich.assert_not_called()
    last_update = progress.update.call_args_list[-1]
    assert last_update.kwargs["ok"] == 0
    assert last_update.kwargs["fail"] == 1
