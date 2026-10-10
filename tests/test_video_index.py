"""Finding a video's scenes chunk by chunk (src/producers/scenes/work.py),
each chunk saved as it's done."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.processing.producer_settings import ProducerSettings
from src.producers.scenes.work import ChunkFailed, index_video_scenes

# How the scenes are found, and their record: the producer's (the server's, as the scheduler read them).
SETTINGS = ProducerSettings(None).settings("scenes")
LINEAGE = ProducerSettings(None).lineage("scenes", None)


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


def _claims(client: MagicMock, responses: list) -> None:
    """Claims (POST .../chunks/next) answer these in turn; other POSTs answer
    client.post.return_value as it is now."""
    answers = iter(responses)
    default = client.post.return_value

    def post(path, *args, **kwargs):
        return next(answers) if path.endswith("/chunks/next") else default

    client.post.side_effect = post


@patch("src.producers.scenes.work.VideoScanner")
@patch("src.producers.scenes.work.SceneSegmenter")
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
    _claims(client, [work_order, done_resp])

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
        lineage=LINEAGE,
        settings=SETTINGS,
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
    assert body["lineage"] == LINEAGE


@patch("src.producers.scenes.work.VideoScanner")
@patch("src.producers.scenes.work.SceneSegmenter")
def test_index_video_scenes_all_complete(mock_segmenter_cls, mock_scanner_cls):
    """When all chunks are already complete, returns 0 scenes."""
    client = MagicMock()
    client.post.return_value = _FakeResponse(data={"chunk_count": 1, "already_initialized": True})

    # Immediate 204 — all done
    _claims(client, [_FakeResponse(204)])

    result = index_video_scenes(
        client=client,
        source_path=Path("/fake/video.mp4"),
        asset_id="asset_1",
        duration_sec=30.0,
        rel_path="video.mp4",
        lineage=LINEAGE,
        settings=SETTINGS,
    )

    assert result["scenes"] == 0
    assert result["chunks"] == 0


@patch("src.producers.scenes.work.VideoScanner")
@patch("src.producers.scenes.work.SceneSegmenter")
def test_index_video_scenes_chunk_failure(mock_segmenter_cls, mock_scanner_cls):
    """A failed chunk is reported to the server and ends the run: the
    video's failure is raised, so the worker reports it (and waits its turn)."""
    from src.processing.video.video_scanner import SyncError

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
    _claims(client, [work_order, done_resp])

    # Scanner raises SyncError
    mock_scanner_cls.return_value.scan.side_effect = SyncError("FFmpeg hung")

    with pytest.raises(ChunkFailed, match="chunk 0 failed: FFmpeg hung"):
        index_video_scenes(
            client=client,
            source_path=Path("/fake/video.mp4"),
            asset_id="asset_1",
            duration_sec=30.0,
            rel_path="video.mp4",
            lineage=LINEAGE,
            settings=SETTINGS,
        )

    # Verify fail was posted
    fail_calls = [c for c in client.post.call_args_list if "fail" in str(c)]
    assert len(fail_calls) == 1
    fail_body = fail_calls[0].kwargs["json"]
    assert fail_body["worker_id"] == "vid_abc"
    assert "FFmpeg hung" in fail_body["error_message"]


@patch("src.producers.scenes.work.VideoScanner")
@patch("src.producers.scenes.work.SceneSegmenter")
def test_index_video_scenes_multi_chunk(mock_segmenter_cls, mock_scanner_cls):
    """Two chunks, each completed with its scenes (the server numbers them)."""
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
    _claims(client, [chunk0, chunk1, done_resp])

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
        lineage=LINEAGE,
        settings=SETTINGS,
    )

    assert result["scenes"] == 2
    assert result["chunks"] == 2

    # Verify both completes were called with correct chunk_ids
    complete_calls = [c for c in client.post.call_args_list if "complete" in str(c)]
    assert len(complete_calls) == 2
    assert [c.args[0] for c in complete_calls] == ["/v1/video/chunks/chunk_0/complete",
                                                   "/v1/video/chunks/chunk_1/complete"]
    assert all("scene_index" not in c.kwargs["json"]["scenes"][0] for c in complete_calls)


@patch("src.producers.scenes.work.VideoScanner")
@patch("src.producers.scenes.work.SceneSegmenter")
def test_index_video_scenes_overlap_calculation(mock_segmenter_cls, mock_scanner_cls):
    """Scanner is called with start_ts - overlap for anchor continuity."""
    client = MagicMock()
    client.post.return_value = _FakeResponse(data={"chunk_count": 1, "already_initialized": False})

    work = _FakeResponse(200, {
        "chunk_id": "c1", "worker_id": "w1", "chunk_index": 1,
        "start_ts": 30.0, "end_ts": 60.0, "overlap_sec": 2.0,
        "anchor_phash": "abc", "scene_start_ts": None,
    })
    _claims(client, [work, _FakeResponse(204)])
    mock_scanner_cls.return_value.scan.return_value = iter([])
    mock_segmenter_cls.return_value = _FakeSegmenter([])

    index_video_scenes(
        client=client,
        source_path=Path("/fake/video.mp4"),
        asset_id="asset_1",
        duration_sec=60.0,
        rel_path="video.mp4",
        lineage=LINEAGE,
        settings=SETTINGS,
    )

    # Scanner should be called with start_ts=28.0 (30.0 - 2.0), end_ts=60.0
    mock_scanner_cls.return_value.scan.assert_called_once_with(28.0, 60.0)

    # Segmenter should receive the anchor_phash from work order
    mock_segmenter_cls.assert_called_once()
    _, kwargs = mock_segmenter_cls.call_args
    assert kwargs.get("anchor_phash") == "abc"


@patch("src.producers.scenes.work.VideoScanner")
@patch("src.producers.scenes.work.SceneSegmenter")
def test_index_video_scenes_scene_fields_complete(mock_segmenter_cls, mock_scanner_cls):
    """All scene fields are passed through to the server."""
    client = MagicMock()
    client.post.return_value = _FakeResponse(data={"chunk_count": 1, "already_initialized": False})

    work = _FakeResponse(200, {
        "chunk_id": "c1", "worker_id": "w1", "chunk_index": 0,
        "start_ts": 0.0, "end_ts": 30.0, "overlap_sec": 2.0,
    })
    _claims(client, [work, _FakeResponse(204)])
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
        lineage=LINEAGE,
        settings=SETTINGS,
    )

    complete_call = [c for c in client.post.call_args_list if "complete" in str(c)]
    result_scene = complete_call[0].kwargs["json"]["scenes"][0]
    assert result_scene["start_ms"] == 5000
    assert result_scene["end_ms"] == 25000
    assert result_scene["rep_frame_ms"] == 12000
    assert result_scene["sharpness_score"] == 42.5
    assert result_scene["keep_reason"] == "phash"
    assert result_scene["phash"] == "deadbeef"
    assert "scene_index" not in result_scene  # the server numbers them


def _work(index: int) -> _FakeResponse:
    return _FakeResponse(200, {"chunk_id": f"chunk_{index}", "worker_id": "vid_w", "chunk_index": index,
                               "start_ts": 30.0 * index, "end_ts": 30.0 * (index + 1), "overlap_sec": 2.0})


def _claimed(client: MagicMock) -> int:
    return sum(1 for c in client.post.call_args_list if c.args and c.args[0].endswith("/chunks/next"))


def _completing_raises(client: MagicMock, error: BaseException) -> None:
    init = _FakeResponse(data={"chunk_count": 1, "already_initialized": False})
    answers = iter([_work(0), _FakeResponse(204)])

    def post(path, *args, **kwargs):
        if path.endswith("/chunks/next"):
            return next(answers)
        if path.endswith("/complete"):
            raise error
        return init

    client.post.side_effect = post


def _index(client: MagicMock, **kw) -> dict:
    return index_video_scenes(client=client, source_path=Path("/fake/video.mp4"), asset_id="asset_1",
                              duration_sec=kw.pop("duration_sec", 30.0), rel_path="video.mp4", lineage=LINEAGE,
                              settings=SETTINGS, **kw)


@patch("src.producers.scenes.work.VideoScanner")
@patch("src.producers.scenes.work.SceneSegmenter")
def test_a_failed_chunk_ends_the_run(mock_segmenter_cls, mock_scanner_cls):
    """The server hands a failed chunk straight back: the run ends at the
    first failed chunk, and nothing more is claimed."""
    from src.processing.video.video_scanner import SyncError

    client = MagicMock()
    client.post.return_value = _FakeResponse(data={"chunk_count": 3, "already_initialized": False})
    _claims(client, [_work(0), _work(1), _work(2), _FakeResponse(204)])
    mock_scanner_cls.return_value.scan.side_effect = SyncError("FFmpeg hung")
    with pytest.raises(ChunkFailed):
        _index(client, duration_sec=90.0)
    assert _claimed(client) == 1


@patch("src.producers.scenes.work.VideoScanner")
@patch("src.producers.scenes.work.SceneSegmenter")
def test_a_chunk_that_cant_be_saved_is_let_go_of_and_the_error_goes_up_as_it_is(mock_segmenter_cls,
                                                                                  mock_scanner_cls):
    """Saving a chunk failing isn't the chunk's fault: the runner judges the
    API's error; the chunk is failed so the next turn starts from it."""
    from src.processing.api import LumiverbAPIError

    client = MagicMock()
    _completing_raises(client, LumiverbAPIError("unavailable", "database away", 503))
    mock_scanner_cls.return_value.scan.return_value = iter([])
    mock_segmenter_cls.return_value = _FakeSegmenter([_FakeScene(0, 30000, 1000)])
    with pytest.raises(LumiverbAPIError) as raised:
        _index(client)
    assert raised.value.status_code == 503
    fails = [c for c in client.post.call_args_list if c.args[0] == "/v1/video/chunks/chunk_0/fail"]
    assert len(fails) == 1 and fails[0].kwargs["json"]["worker_id"] == "vid_w"


@patch("src.producers.scenes.work.VideoScanner")
@patch("src.producers.scenes.work.SceneSegmenter")
def test_a_chunk_stopped_while_saving_isnt_failed(mock_segmenter_cls, mock_scanner_cls):
    """Stopping isn't the chunk's trouble: no /fail (its lease runs out), Stopped goes up."""
    from src.producers.runner import Stopped

    client = MagicMock()
    _completing_raises(client, Stopped("/v1/video/chunks/chunk_0/complete"))
    mock_scanner_cls.return_value.scan.return_value = iter([])
    mock_segmenter_cls.return_value = _FakeSegmenter([_FakeScene(0, 30000, 1000)])
    with pytest.raises(Stopped):
        _index(client)
    assert not [c for c in client.post.call_args_list if c.args[0].endswith("/fail")]


@patch("src.producers.scenes.work.VideoScanner")
@patch("src.producers.scenes.work.SceneSegmenter")
def test_a_chunk_that_cant_be_let_go_of_still_raises_the_saving_error(mock_segmenter_cls, mock_scanner_cls):
    from src.processing.api import LumiverbAPIError

    client = MagicMock()
    error = LumiverbAPIError("unavailable", "database away", 503)
    init = _FakeResponse(data={"chunk_count": 1, "already_initialized": False})
    answers = iter([_work(0)])

    def post(path, *args, **kwargs):
        if path.endswith("/chunks/next"):
            return next(answers)
        if path.endswith("/complete"):
            raise error
        if path.endswith("/fail"):
            raise LumiverbAPIError("unavailable", "still away", 503)
        return init

    client.post.side_effect = post
    mock_scanner_cls.return_value.scan.return_value = iter([])
    mock_segmenter_cls.return_value = _FakeSegmenter([])
    with pytest.raises(LumiverbAPIError) as raised:
        _index(client)
    assert raised.value is error


@patch("src.producers.scenes.work.VideoScanner")
def test_once_stopping_no_chunk_is_claimed(mock_scanner_cls):
    import threading

    from src.producers.runner import Stopped

    client = MagicMock()
    client.post.return_value = _FakeResponse(data={"chunk_count": 3, "already_initialized": False})
    _claims(client, [_work(0)])
    stopping = threading.Event()
    stopping.set()
    with pytest.raises(Stopped):
        _index(client, duration_sec=90.0, stopping=stopping)
    assert _claimed(client) == 0
