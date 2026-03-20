from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.storage.artifact_store import ArtifactRef
from src.video.video_scanner import RawFrame
from src.workers.video_index_worker import VideoIndexWorker


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


@pytest.mark.fast
def test_video_index_process_writes_thumbnail_via_artifact_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fake-video")
    thumb_bytes = b"\xff\xd8\xffthumb"

    def _fake_extract(_source: Path, out_path: Path, timestamp: float) -> bool:
        assert timestamp == 0.0
        out_path.write_bytes(thumb_bytes)
        return True

    monkeypatch.setattr("src.workers.video_index_worker.extract_video_frame", _fake_extract)

    artifact_store = MagicMock()
    artifact_store.write_artifact.return_value = ArtifactRef(
        key="t/lib/thumbnails/00/ast_01_thumb.jpg",
        sha256="abc",
    )

    def _raw(method: str, path: str, **kwargs: object) -> _Resp:
        if method == "POST" and path.endswith("/chunks"):
            return _Resp(200, {"chunk_count": 1, "already_initialized": False})
        if method == "POST" and path.endswith("/thumbnail-key"):
            return _Resp(200, {})
        if method == "GET" and path.endswith("/chunks/next"):
            return _Resp(204, {})
        raise AssertionError(f"Unexpected call: {method} {path} {kwargs}")

    client = MagicMock()
    client.raw.side_effect = _raw
    worker = VideoIndexWorker(client=client, artifact_store=artifact_store, once=True)
    result = worker.process(
        {
            "asset_id": "ast_01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "library_id": "lib_01",
            "root_path": str(tmp_path),
            "rel_path": "clip.mp4",
            "media_type": "video",
            "duration_sec": 1.0,
        }
    )

    assert result == {}
    artifact_store.write_artifact.assert_called_once_with(
        "thumbnail",
        "ast_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        thumb_bytes,
        library_id="lib_01",
        rel_path="clip.mp4",
    )


@pytest.mark.fast
def test_video_index_process_chunk_writes_scene_rep_via_artifact_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    proxy_path = tmp_path / "proxy.mp4"
    proxy_path.write_bytes(b"proxy")
    captured_complete: dict = {}

    class _FakeScanner:
        def __init__(self, _proxy_path: Path) -> None:
            pass

        def scan(self, start_ts: float, end_ts: float):
            assert start_ts == 0.0
            assert end_ts == 2.0
            yield RawFrame(bytes=b"\x00" * 9, pts=0.2, width=3, height=3)

    class _FakeSegmenter:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def segment(self):
            return [
                SimpleNamespace(
                    start_ms=100,
                    end_ms=900,
                    rep_frame_ms=400,
                    sharpness_score=0.8,
                    keep_reason="motion",
                    phash="phash-1",
                )
            ]

        next_anchor_phash = "anchor-next"
        next_scene_start_ms = 950

    def _fake_extract(_source: Path, out_path: Path, timestamp: float) -> bool:
        assert timestamp == 0.4
        out_path.write_bytes(b"\xff\xd8\xffscene")
        return True

    monkeypatch.setattr("src.workers.video_index_worker.VideoScanner", _FakeScanner)
    monkeypatch.setattr("src.workers.video_index_worker.SceneSegmenter", _FakeSegmenter)
    monkeypatch.setattr("src.workers.video_index_worker.extract_video_frame", _fake_extract)

    artifact_store = MagicMock()
    artifact_store.write_artifact.return_value = ArtifactRef(
        key="t/lib/scenes/00/ast_01_0000000400.jpg",
        sha256="scene-sha",
    )
    worker = VideoIndexWorker(client=MagicMock(), artifact_store=artifact_store, once=True)

    def _request(method: str, path: str, **kwargs: object) -> _Resp:
        if method == "POST" and path.endswith("/complete"):
            captured_complete.update(kwargs["json"])
            return _Resp(200, {"all_complete": False})
        raise AssertionError(f"Unexpected request: {method} {path}")

    worker._request = _request  # type: ignore[assignment]
    worker._process_chunk(
        source=source,
        proxy_path=proxy_path,
        chunk_offset=0.0,
        work_order={
            "chunk_id": "chk_1",
            "worker_id": "w_1",
            "start_ts": 0.0,
            "end_ts": 2.0,
            "chunk_index": 0,
            "anchor_phash": None,
            "scene_start_ts": None,
        },
        tmpdir=tmp_path,
        library_id="lib_01",
        asset_id="ast_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        rel_path="clip.mp4",
    )

    artifact_store.write_artifact.assert_called_once_with(
        "scene_rep",
        "ast_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        b"\xff\xd8\xffscene",
        library_id="lib_01",
        rel_path="clip.mp4",
        rep_frame_ms=400,
    )
    assert captured_complete["scenes"][0]["thumbnail_key"] == "t/lib/scenes/00/ast_01_0000000400.jpg"
    assert captured_complete["scenes"][0]["rep_frame_sha256"] == "scene-sha"
