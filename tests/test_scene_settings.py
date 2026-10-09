"""Scene detection reads the account's settings (Settings → Processing → Scenes):
the change threshold, the longest and shortest scene, and the frame width."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import src.client.video.video_scanner as vs_module
from src.client.video.scene_segmenter import SceneSegmenter, SceneSettings
from src.client.video.video_scanner import RawFrame, VideoScanner
from src.producers.scenes import PRODUCER as SCENES


def _frames(*pts: float) -> list[RawFrame]:
    return [RawFrame(bytes=b"\x00" * 12, pts=p, width=2, height=2) for p in pts]


def _segment(frames, settings: SceneSettings | None = None, distance: int = 0) -> list:
    with (
        patch("src.client.video.scene_segmenter._frame_to_phash", return_value="00" * 32),
        patch("src.client.video.scene_segmenter._frame_sharpness", return_value=1.0),
        patch("src.client.video.scene_segmenter._hamming_hex", return_value=distance),
    ):
        return SceneSegmenter(frames, settings=settings).segment()


@pytest.mark.fast
def test_the_defaults_are_the_producers_declared_defaults():
    s = SceneSettings()
    for key in ("phash_threshold", "phash_hash_size", "temporal_ceiling_sec", "debounce_sec", "frame_width"):
        assert getattr(s, key) == SCENES.defaults[key], key


@pytest.mark.fast
def test_settings_come_from_the_producers_settings_and_ignore_the_rest():
    s = SceneSettings.for_producer({"phash_threshold": 30, "temporal_ceiling_sec": 12.5, "frames": "keyframes",
                                    "something_new": 1})
    assert s.phash_threshold == 30
    assert s.temporal_ceiling_sec == 12.5
    assert s.debounce_sec == SCENES.defaults["debounce_sec"]


@pytest.mark.fast
def test_a_lower_threshold_finds_a_change_the_default_doesnt():
    frames = _frames(0, 4, 8)
    assert len(_segment(frames, distance=40)) == 1  # 40 ≤ 51: one scene
    lower = _segment(frames, SceneSettings(phash_threshold=30), distance=40)
    assert [s.keep_reason for s in lower] == ["phash", "phash", "forced"]


@pytest.mark.fast
def test_the_longest_scene_closes_one_on_time():
    frames = _frames(*range(13))
    scenes = _segment(frames, SceneSettings(temporal_ceiling_sec=5.0))
    assert [(s.start_ms, s.keep_reason) for s in scenes] == [
        (0, "temporal"), (5000, "temporal"), (10000, "forced")]


@pytest.mark.fast
def test_the_shortest_scene_holds_off_a_change():
    frames = _frames(0, 1, 2, 3)
    assert len(_segment(frames, distance=60)) == 2  # 3 s by default: one change, at 3
    every = _segment(frames, SceneSettings(debounce_sec=0.5), distance=60)
    assert [s.start_ms for s in every] == [0, 1000, 2000, 3000]


@pytest.mark.fast
def test_the_hash_size_is_the_one_hashed_with():
    import src.client.video.scene_segmenter as seg

    raw = RawFrame(bytes=b"\x00" * (8 * 8 * 3), pts=0.0, width=8, height=8)
    with patch.object(seg.imagehash, "phash", return_value="h") as phash:
        seg._frame_to_phash(raw, 8)
    assert phash.call_args.kwargs["hash_size"] == 8


@pytest.fixture()
def scanner_of(tmp_path, monkeypatch):
    monkeypatch.setattr(vs_module, "PTS_QUEUE_TIMEOUT", 1.0)
    monkeypatch.setattr(vs_module, "PTS_QUEUE_POLL_INTERVAL", 0.05)
    video = tmp_path / "v.mp4"
    video.touch()

    def make(size: tuple[int, int], width: int | None):
        with patch("src.client.video.video_scanner._get_video_size", return_value=size):
            return VideoScanner(video, width=width)
    return make


def _scan_once(scanner: VideoScanner, frame_bytes: int) -> tuple[list[str], list[RawFrame]]:
    proc = MagicMock()
    proc.stdout.read.side_effect = [bytes(frame_bytes), b""]
    proc.poll.return_value = None
    proc.returncode = 0
    proc.stderr = iter([b"[Parsed_showinfo_1] n:0 pts_time:0.5 \n"])
    with patch("subprocess.Popen", return_value=proc) as popen:
        frames = list(scanner.scan(0.0, 10.0))
    return popen.call_args.args[0], frames


@pytest.mark.fast
def test_frames_are_scaled_to_the_frame_width(scanner_of):
    cmd, frames = _scan_once(scanner_of((960, 540), 480), 480 * 270 * 3)
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=480:270" in vf
    assert [(f.width, f.height, len(f.bytes)) for f in frames] == [(480, 270, 480 * 270 * 3)]


@pytest.mark.fast
def test_a_video_narrower_than_the_frame_width_isnt_scaled_up(scanner_of):
    cmd, frames = _scan_once(scanner_of((320, 180), 480), 320 * 180 * 3)
    assert "scale=" not in cmd[cmd.index("-vf") + 1]
    assert [(f.width, f.height) for f in frames] == [(320, 180)]


@pytest.mark.fast
def test_an_odd_scaled_height_is_made_even(scanner_of):
    cmd, frames = _scan_once(scanner_of((1000, 563), 480), 480 * 270 * 3)
    assert "scale=480:270" in cmd[cmd.index("-vf") + 1]
    assert frames[0].height == 270


# ── The indexer and the scheduler use them ───────────────────────────────


class _Resp:
    def __init__(self, status_code: int = 200, data: dict | None = None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


def _client_with_no_chunks_left(producers: list[dict] | None = None) -> MagicMock:
    client = MagicMock()
    client.post.return_value = _Resp(data={"chunk_count": 1, "already_initialized": True})
    client.raw.return_value = _Resp(204)
    client.get.return_value = _Resp(data={"producers": producers or []})
    return client


def _index(client, **kwargs):
    from src.client.cli.video_index import index_video_scenes

    with (
        patch("src.client.cli.video_index.VideoScanner") as scanner_cls,
        patch("src.client.cli.video_index.SceneSegmenter") as segmenter_cls,
    ):
        index_video_scenes(client=client, source_path=MagicMock(), asset_id="ast_1", duration_sec=30.0,
                           rel_path="v.mp4", **kwargs)
    return scanner_cls, segmenter_cls


@pytest.mark.fast
def test_the_indexer_finds_scenes_with_the_settings_its_given():
    client = _client_with_no_chunks_left()
    client.raw.side_effect = [_Resp(200, {"chunk_id": "c", "worker_id": "w", "chunk_index": 0, "start_ts": 0.0,
                                          "end_ts": 30.0}), _Resp(204)]
    used = {**SCENES.defaults, "frame_width": 320, "phash_threshold": 20}
    with (
        patch("src.client.cli.video_index.VideoScanner") as scanner_cls,
        patch("src.client.cli.video_index.SceneSegmenter") as segmenter_cls,
    ):
        segmenter_cls.return_value.segment.return_value = []
        from src.client.cli.video_index import index_video_scenes

        index_video_scenes(client=client, source_path=MagicMock(), asset_id="ast_1", duration_sec=30.0,
                           rel_path="v.mp4", lineage={"producer": "scene-detect"}, settings=used)
    assert scanner_cls.call_args.kwargs["width"] == 320
    assert segmenter_cls.call_args.kwargs["settings"] == SceneSettings.for_producer(used)


@pytest.mark.fast
def test_without_settings_it_reads_the_servers_once_for_both_the_settings_and_the_lineage():
    from src.shared.producers import lineage

    server = {**SCENES.defaults, "frame_width": 640}
    client = _client_with_no_chunks_left([{"artifact": "scenes", "settings": server}])
    scanner_cls, _ = _index(client)
    assert scanner_cls.call_args.kwargs["width"] == 640
    init = next(c for c in client.post.call_args_list if c.args[0] == "/v1/video/ast_1/chunks")
    assert init.kwargs["json"] == {"duration_sec": 30.0}
    assert client.get.call_count == 1
    assert lineage("scenes", server, None)["settings_hash"] != lineage("scenes", SCENES.defaults, None)["settings_hash"]


@pytest.mark.fast
def test_a_redo_asks_the_server_to_start_the_clip_over_saying_how_its_scenes_are_found():
    client = _client_with_no_chunks_left()
    made = {"producer": "scene-detect", "version": "1", "settings_hash": "abc", "source_sha256": None}
    _index(client, lineage=made, settings=SCENES.defaults, redo=True)
    init = next(c for c in client.post.call_args_list if c.args[0] == "/v1/video/ast_1/chunks")
    assert init.kwargs["json"] == {"duration_sec": 30.0, "redo": True, "lineage": made}


@pytest.mark.fast
def test_the_scheduler_hands_the_indexer_the_settings_its_lineage_names_and_each_clips_redo():
    from src.server.scheduler import runners
    from src.shared.producers import lineage

    acct = MagicMock()
    acct.stopping.is_set.return_value = False
    used = {**SCENES.defaults, "debounce_sec": 1.0}
    acct.producers.settings.return_value = used
    acct.producers.lineage.side_effect = lambda artifact, sha, used=None: lineage(artifact, used, sha)
    job = MagicMock()
    job.items = [{"asset_id": "a1", "rel_path": "a.mp4", "duration_sec": 10.0, "sha256": "s1", "redo": True},
                 {"asset_id": "a2", "rel_path": "b.mp4", "duration_sec": 10.0, "sha256": "s2"}]
    job.asset_ids = ["a1", "a2"]
    with patch("src.client.cli.video_index.index_video_scenes") as index:
        acct.analysis_cache.get.return_value = MagicMock(is_file=lambda: True)
        index.return_value = {"scenes": 1, "chunks": 1, "elapsed": 0.1}
        runners.scenes(acct, job)
    calls = {c.kwargs["asset_id"]: c.kwargs for c in index.call_args_list}
    assert calls["a1"]["settings"] == used and calls["a2"]["settings"] == used
    assert calls["a1"]["lineage"] == lineage("scenes", used, "s1")
    assert (calls["a1"]["redo"], calls["a2"]["redo"]) == (True, False)
    acct.producers.settings.assert_called_once_with("scenes")


@pytest.mark.fast
def test_a_scene_is_never_longer_than_the_chunk_its_found_in():
    """Videos are looked at 30 seconds at a time and each chunk closes its last
    scene: a longer "longest scene" would change nothing yet redo every clip."""
    from src.server.repository.tenant import VideoIndexChunkRepository

    ceiling, debounce = SCENES.setting("temporal_ceiling_sec"), SCENES.setting("debounce_sec")
    assert ceiling.maximum <= VideoIndexChunkRepository.CHUNK_DURATION_SEC
    assert debounce.maximum < ceiling.minimum  # or a change could never start a scene


@pytest.mark.fast
def test_a_scene_dropped_while_its_described_is_skipped_not_failed(tmp_path):
    from src.client.cli.client import LumiverbAPIError
    from src.client.cli.video_index import run_video_enrich

    client = MagicMock()
    client.get.return_value.json.return_value = {"scenes": [{"scene_id": "scn_1", "rep_frame_ms": 0}]}
    source = tmp_path / "v.mp4"
    source.write_bytes(b"x")
    failed: list = []
    with patch("src.client.cli.video_index.enrich_scene",
               side_effect=LumiverbAPIError("scene_gone", "found again", 409)):
        ok, fail = run_video_enrich(client=client, source_for=lambda v: source,
                                    videos=[{"asset_id": "a1", "rel_path": "v.mp4"}], vision_provider=MagicMock(),
                                    vision_model_id="m", console=MagicMock(), progress=MagicMock(),
                                    task_id=None, on_fail=lambda a, e: failed.append(a))
    assert failed == [] and fail == 0
