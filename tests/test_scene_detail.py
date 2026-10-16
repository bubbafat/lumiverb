"""Scene descriptions by Detail (src/producers/scenes/detail.py): how long a
scene is for a video's length, the chunks cut in whole scene windows, real
cuts inside a window, each scene's picture, and the discard pass that merges
scenes alike. Synthetic keyframes, 2 s apart as in an analysis copy, in
video time (VideoScanner.scan's contract); the scenarios hash real pictures."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import src.processing.video.video_scanner as vs_module
from src.processing.video.scene_segmenter import CUT, WINDOW, SceneSegmenter, spread
from src.processing.video.video_scanner import RawFrame, VideoScanner
from src.producers.scenes import PRODUCER as SCENES
from src.producers.scenes.detail import (
    CHUNK_TARGET_SEC,
    CUT_MIN_SEC,
    DETAILS,
    LEVELS,
    SAMPLES,
    chunk_grid,
    detail,
    hamming,
    merge_runs,
    scene_length,
    window_sec,
)

pytestmark = pytest.mark.fast

W, H = 96, 64
_rng = np.random.default_rng(7)


def _picture(seed: int) -> np.ndarray:
    """A smooth picture of its own (unrelated pictures' hashes are ~128 bits apart)."""
    rng = np.random.default_rng(seed)
    coarse = rng.integers(0, 256, size=(6, 8, 3)).astype(np.float32)
    return np.kron(coarse, np.ones((H // 6 + 1, W // 8, 1)))[:H, :W].astype(np.uint8)


def _talking(t: float) -> np.ndarray:
    """A person talking to camera: one set-up, a mouth that moves, the light
    shifting a little, sensor noise. Keyframes 2 s apart come out about 0 to
    20 bits apart, within what real talking heads measured (median 10, 90th
    percentile about 30)."""
    img = _picture(99).astype(int)  # the room behind them
    yy, xx = np.ogrid[:H, :W]
    img[(xx - W // 2) ** 2 / 14**2 + (yy - H // 2) ** 2 / 18**2 <= 1] = (200, 160, 140)  # the head
    img[H // 2 + 8: H // 2 + 8 + int(2 + 2 * abs(np.sin(t))), W // 2 - 4: W // 2 + 4] = (90, 40, 40)  # the mouth
    img += int(2 * np.sin(t / 7)) + _rng.integers(-3, 4, size=img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def _frames(duration: float, picture, step: float = 2.0) -> list[RawFrame]:
    out, t = [], 0.0
    while t < duration:
        out.append(RawFrame(bytes=picture(t).tobytes(), pts=t, width=W, height=H))
        t += step
    return out


def _video(duration: float, picture, level: str) -> tuple[list, list[tuple[int, int]]]:
    """Every chunk's scenes as the indexer finds them, then the runs the discard pass merges."""
    frames, window = _frames(duration, picture), window_sec(duration, level)
    grid = chunk_grid(duration, window)
    scenes = []
    for i, (a, b) in enumerate(grid):
        chunk = [f for f in frames if a / 1000 <= f.pts < b / 1000]  # scan() is [start, end)
        scenes += SceneSegmenter(chunk, start=a / 1000, end=b / 1000, window_sec=window,
                                 last=i == len(grid) - 1).segment()
    return scenes, merge_runs([s.phash for s in scenes], detail(level).same_within)


# ── How long a scene is ───────────────────────────────────────────────────


@pytest.mark.parametrize(("level", "duration", "window"), [
    # target 4, 5 to 120 s
    ("low", 3, 3.0), ("low", 10, 5.0), ("low", 30, 7.5), ("low", 60, 15.0), ("low", 300, 75.0),
    ("low", 1800, 120.0),
    # target 8, 5 to 60 s
    ("normal", 3, 3.0), ("normal", 10, 5.0), ("normal", 30, 5.0), ("normal", 60, 7.5), ("normal", 300, 37.5),
    ("normal", 1800, 60.0),
    # target 16, 3 to 30 s
    ("high", 3, 3.0), ("high", 10, 10 / 3), ("high", 30, 3.0), ("high", 60, 3.75), ("high", 300, 18.75),
    ("high", 1800, 30.0),
])
def test_a_scene_scales_with_the_video_within_the_levels_bounds(level, duration, window):
    assert window_sec(duration, level) == pytest.approx(window)


def test_the_levels_are_as_decided():
    assert DETAILS == ("low", "normal", "high")
    assert [(d.target_scenes, d.min_sec, d.max_sec) for d in LEVELS.values()] == [
        (4, 5, 120), (8, 5, 60), (16, 3, 30)]
    # Low merges more (looser), High keeps more (stricter).
    assert LEVELS["low"].same_within > LEVELS["normal"].same_within > LEVELS["high"].same_within
    assert SAMPLES == 5


def test_the_length_asked_is_clamped_then_evened_out_to_whole_windows():
    assert scene_length(22, "normal") == 5.0  # 22 / 8 = 2.75, at least 5
    assert window_sec(22, "normal") == pytest.approx(22 / 4)  # four windows of 5.5, not four of 5 and a 2 s scrap
    assert scene_length(3600, "normal") == 60.0 and window_sec(3600, "normal") == 60.0  # long: bounded, no special case


@pytest.mark.parametrize("level", DETAILS)
@pytest.mark.parametrize("duration", [2.5, 7.1, 22, 61, 95.5, 286.9, 697.2, 1800, 7200])
def test_chunks_are_whole_windows_so_no_scene_is_cut_short_at_a_chunks_end(level, duration):
    window = window_sec(duration, level)
    grid = chunk_grid(duration, window)
    assert grid[0][0] == 0 and grid[-1][1] == round(duration * 1000)
    assert all(a[1] == b[0] for a, b in zip(grid, grid[1:]))  # end to start
    for start, end in grid:
        windows = (end - start) / 1000 / window
        assert windows == pytest.approx(round(windows), abs=1e-3) and round(windows) >= 1
        assert (end - start) / 1000 <= max(CHUNK_TARGET_SEC * 1.5, window * 1.0001)


# ── A chunk's scenes ──────────────────────────────────────────────────────


def _bits(n: int) -> str:
    """A 256-bit hash with its n lowest bits set: n bits from _bits(0)."""
    return f"{(1 << n) - 1:064x}"


def _segment(pts_hash: dict[float, str], *, start=0.0, end=30.0, window=5.0, sharp=None, last=True):
    frames = [RawFrame(bytes=b"", pts=p, width=1, height=1) for p in pts_hash]
    with (patch("src.processing.video.scene_segmenter._frame_to_phash", side_effect=lambda f: pts_hash[f.pts]),
          patch("src.processing.video.scene_segmenter._frame_sharpness",
                side_effect=lambda f: (sharp or {}).get(f.pts, 1.0)) as sharpness):
        return SceneSegmenter(frames, start=start, end=end, window_sec=window, last=last).segment(), sharpness


def test_each_window_is_a_scene_and_they_cover_the_chunk_end_to_end():
    scenes, _ = _segment({t: _bits(0) for t in range(30, 60, 2)}, start=30, end=60, window=5)
    assert [(s.start_ms, s.end_ms) for s in scenes] == [(30000 + 5000 * i, 35000 + 5000 * i) for i in range(6)]
    assert {s.keep_reason for s in scenes} == {WINDOW}


def test_frames_outside_the_chunk_are_left_out():
    scenes, _ = _segment({28.0: _bits(200), 30.0: _bits(0), 32.0: _bits(0), 34.0: _bits(0), 35.0: _bits(0)},
                         start=30, end=35, window=5, last=False)
    assert len(scenes) == 1 and scenes[0].rep_frame_ms >= 30000


def test_a_scenes_picture_is_the_sharpest_of_five_keyframes_spread_across_it():
    pts = [float(t) for t in range(0, 60, 2)]  # 30 keyframes in one 60 s window
    sharp = {t: t for t in pts}  # later is sharper...
    scenes, sharpness = _segment({t: _bits(0) for t in pts}, end=60, window=60, sharp=sharp)
    looked_at = sorted(c.args[0].pts for c in sharpness.call_args_list)
    assert len(looked_at) == SAMPLES and looked_at[0] > 0 and looked_at[-1] < 58  # spread, not the edges
    assert scenes[0].rep_frame_ms == int(looked_at[-1] * 1000)  # ...the sharpest looked at
    assert spread(3) == [0, 1, 2] and spread(10) == [1, 3, 5, 7, 9]


def test_the_scenes_hash_is_its_pictures():
    pts = {0.0: _bits(0), 2.0: _bits(3), 4.0: _bits(5)}
    scenes, _ = _segment(pts, end=6, window=6, sharp={2.0: 9.0})
    assert scenes[0].rep_frame_ms == 2000 and scenes[0].phash == _bits(3)


def test_a_cut_whose_picture_holds_splits_a_window():
    # One 30 s window: shot A to 12 s, then shot B, which holds.
    pts = {float(t): (_bits(0) if t < 12 else _bits(200)) for t in range(0, 30, 2)}
    scenes, _ = _segment(pts, end=30, window=30)
    assert [(s.start_ms, s.end_ms, s.keep_reason) for s in scenes] == [(0, 12000, CUT), (12000, 30000, WINDOW)]


def test_a_moving_camera_isnt_a_cut():
    # Every keyframe far from the one before (handheld, 2 s apart): no picture holds.
    rng = np.random.default_rng(3)
    pts = {float(t): f"{int.from_bytes(rng.bytes(32), 'big'):064x}" for t in range(0, 30, 2)}
    assert all(hamming(pts[t], pts[t + 2]) > 76 for t in range(0, 28, 2)), "the frames move"
    scenes, _ = _segment(pts, end=30, window=30)
    assert len(scenes) == 1


def test_no_sliver_either_side_of_a_cut():
    # Shot B starts 1 s before the window ends: too close, no cut (the window's end is near enough).
    pts = {0.0: _bits(0), 2.0: _bits(0), 4.0: _bits(0), 6.0: _bits(200), 6.5: _bits(200)}
    scenes, _ = _segment(pts, end=7, window=7)
    assert len(scenes) == 1 and CUT_MIN_SEC == 2.0


def test_a_window_without_a_keyframe_joins_the_next_and_at_the_end_the_one_before():
    pts = {6.0: _bits(0), 8.0: _bits(0), 12.0: _bits(0)}  # nothing in [0, 5)
    scenes, _ = _segment(pts, end=20, window=5)
    assert [(s.start_ms, s.end_ms) for s in scenes] == [(0, 10000), (10000, 20000)]


def test_a_chunk_without_keyframes_has_no_scenes():
    assert _segment({}, end=30, window=5)[0] == []


# ── The discard pass ──────────────────────────────────────────────────────


def test_scenes_alike_merge_into_the_first():
    assert merge_runs([_bits(0), _bits(10), _bits(20), _bits(200), _bits(205)], 40) == [(0, 2), (3, 4)]


def test_a_slow_drift_isnt_chained_into_one_scene():
    # Each scene 12 bits from its neighbour, so neighbour by neighbour it's all one;
    # against the kept scene, a run ends once it's drifted past the level's bound.
    drift = [_bits(12 * i) for i in range(10)]
    assert all(hamming(a, b) == 12 for a, b in zip(drift, drift[1:]))
    assert merge_runs(drift, LEVELS["normal"].same_within) == [(0, 3), (4, 7), (8, 9)]
    assert len(merge_runs(drift, LEVELS["high"].same_within)) > len(merge_runs(drift, LEVELS["low"].same_within))


def test_a_scene_without_a_hash_is_never_merged():
    assert merge_runs([_bits(0), None, _bits(0)], 40) == [(0, 0), (1, 1), (2, 2)]
    assert merge_runs([], 40) == []


# ── Scenarios, with real pictures ─────────────────────────────────────────


@pytest.mark.parametrize("level", DETAILS)
def test_a_talking_head_is_one_scene(level):
    scenes, runs = _video(240, _talking, level)
    assert len(scenes) >= 4  # found window by window...
    assert len(runs) == 1  # ...and kept as one


def test_b_roll_keeps_its_cuts():
    # 120 s at Low: four 30 s windows; shots cut at 20 s and 50 s, inside windows.
    shot = lambda t: _picture(1 if t < 20 else 2 if t < 50 else 3)  # noqa: E731
    scenes, runs = _video(120, shot, "low")
    assert [s.start_ms for s in scenes if s.keep_reason == CUT] == [0, 30000]  # the pieces the cuts end
    assert sorted({s.start_ms for s in scenes}) == [0, 20000, 30000, 50000, 60000, 90000]
    kept = [scenes[a].start_ms for a, _ in runs]
    assert kept == [0, 20000, 50000]  # one scene a shot: each shot's windows merged, the shots kept apart
    assert scenes[runs[-1][1]].end_ms == 120000


def test_more_detail_keeps_more_scenes_of_a_moving_shot():
    moving = lambda t: _picture(1000 + int(t))  # noqa: E731 — a camera on the move: no two keyframes alike
    kept = {level: len(_video(180, moving, level)[1]) for level in DETAILS}
    assert kept == {"low": 4, "normal": 8, "high": 16}  # its windows: target scenes, nothing cut or merged


# ── The producer ──────────────────────────────────────────────────────────


def test_detail_is_the_only_setting_and_its_out_of_the_hash():
    assert [(s.key, s.kind, s.choices, s.remakes) for s in SCENES.settings] == [
        ("detail", "level", DETAILS, False)]
    assert SCENES.defaults == {} and SCENES.use_defaults == {"detail": "normal"}
    assert SCENES.level is SCENES.settings[0] and SCENES.part_of == "scene_vision"
    with pytest.raises(ValueError, match="one of low, normal, high"):
        SCENES.level.check("extreme")


def test_lineage_records_the_level_apart_from_the_hash():
    from src.shared.producers import level_of, lineage

    assert lineage("scenes", {}, None, level_of("scenes", {"detail": "high"}))["level"] == 2
    assert lineage("scenes", {}, None)["level"] == 1  # the default's
    assert lineage("scenes", {}, None, 0)["settings_hash"] == lineage("scenes", {}, None, 2)["settings_hash"]
    assert "level" not in lineage("transcript", {}, None)


@pytest.fixture()
def scanner_of(tmp_path, monkeypatch):
    monkeypatch.setattr(vs_module, "PTS_QUEUE_TIMEOUT", 1.0)
    monkeypatch.setattr(vs_module, "PTS_QUEUE_POLL_INTERVAL", 0.05)
    video = tmp_path / "v.mp4"
    video.touch()

    def make(size: tuple[int, int], width: int | None):
        with patch("src.processing.video.video_scanner._get_video_size", return_value=size):
            return VideoScanner(video, width=width)
    return make


def _scan_once(scanner: VideoScanner, frame_bytes: int) -> tuple[list[str], list[RawFrame]]:
    proc = MagicMock()
    proc.stdout.read.side_effect = [bytes(frame_bytes), b""]
    proc.poll.return_value = None
    proc.returncode = 0
    proc.wait.return_value = 0  # FFmpeg finished cleanly once its output ends
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
# ── The indexer and the scheduler ─────────────────────────────────────────


class _Resp:
    def __init__(self, status_code: int = 200, data: dict | None = None):
        self.status_code = status_code
        self._data = data or {}

    def json(self):
        return self._data


def test_the_scheduler_hands_the_indexer_the_accounts_detail_and_records_its_level(tmp_path):
    from src.producers.runner import run
    from src.producers.scenes.work import Scenes
    from src.server.scheduler.dispatch import Job
    from src.shared.producers import lineage

    acct = MagicMock()
    acct.stopping.is_set.return_value = False
    acct.producers.settings.return_value = {}
    acct.producers.uses.return_value = {"detail": "high"}
    acct.producers.lineage.side_effect = lambda artifact, sha, used=None: lineage(artifact, used, sha)
    proxy = tmp_path / "v.mp4"
    proxy.write_bytes(b"x")
    acct.analysis_cache.get.return_value = proxy
    job = Job("t1", "scenes", 3, ({"asset_id": "a1", "rel_path": "a.mp4", "duration_sec": 10.0, "content_hash": "s1",
                                   "redo": True},))
    with patch("src.producers.scenes.work.index_video_scenes") as index:
        index.return_value = {"scenes": 1, "chunks": 1, "elapsed": 0.1}
        assert run(Scenes, acct, job) is None
    kw = index.call_args.kwargs
    assert kw["detail"] == "high" and kw["redo"] is True
    assert kw["lineage"] == {**lineage("scenes", {}, "s1"), "level": 2}


def test_a_redo_asks_the_server_to_start_the_clip_over_with_its_windows():
    from src.producers.scenes.work import index_video_scenes

    client = MagicMock()
    client.post.side_effect = lambda path, *a, **k: (_Resp(204) if path.endswith("/chunks/next")
                                                     else _Resp(data={"chunk_count": 1, "already_initialized": True}))
    made = {"producer": "scene-detect", "version": "3", "settings_hash": "abc", "source_hash": None, "level": 0}
    with patch("src.producers.scenes.work.VideoScanner"):
        index_video_scenes(client=client, source_path=MagicMock(), asset_id="ast_1", duration_sec=300.0,
                           rel_path="v.mp4", lineage=made, detail="low", redo=True)
    init = next(c for c in client.post.call_args_list if c.args[0] == "/v1/video/ast_1/chunks")
    assert init.kwargs["json"] == {"duration_sec": 300.0, "scene_sec": 75.0, "redo": True, "lineage": made}
