"""Rendering analysis proxies with ffmpeg, and the local cache (ADR-016 phase 2).

An analysis proxy is a full-length, low-resolution copy of a video with its
first audio track. Transcription, scenes and vision read it instead of the
original. These tests render real files made with ffmpeg's test sources.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from src.client.video.analysis_proxy import (
    AnalysisProxySettings,
    RenderError,
    build_command,
    render_analysis_proxy,
)


def _make(path: Path, *, size: str = "1280x720", rate: int = 30, seconds: float = 2.0,
          audio: int = 1, extra: list[str] | None = None) -> Path:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}"]
    for i in range(audio):
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={440 + 220 * i}:duration={seconds}"]
    cmd += ["-map", "0:v"]
    for i in range(audio):
        cmd += ["-map", f"{i + 1}:a"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", *(extra or []), str(path)]
    subprocess.run(cmd, check=True)
    return path


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def _video(info: dict) -> dict:
    return next(s for s in info["streams"] if s["codec_type"] == "video")


def _audio(info: dict) -> list[dict]:
    return [s for s in info["streams"] if s["codec_type"] == "audio"]


def _display_size(stream: dict) -> tuple[int, int]:
    w, h = stream["width"], stream["height"]
    rotation = next((sd.get("rotation") for sd in stream.get("side_data_list", []) if "rotation" in sd), 0)
    return (h, w) if abs(int(rotation or 0)) % 180 == 90 else (w, h)


@pytest.mark.fast
def test_landscape_video_is_scaled_to_960_with_audio(tmp_path: Path) -> None:
    src = _make(tmp_path / "in.mov")
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)

    info = _probe(out)
    v = _video(info)
    assert (v["width"], v["height"]) == (960, 540)
    assert v["codec_name"] == "h264"
    assert len(_audio(info)) == 1
    assert float(info["format"]["duration"]) == pytest.approx(2.0, abs=0.15)


@pytest.mark.fast
def test_rotated_phone_video_comes_out_upright(tmp_path: Path) -> None:
    # Phones record landscape pixels with a rotation flag. The proxy is
    # upright pixels with no flag, so frame readers can't get it wrong.
    flat = _make(tmp_path / "flat.mp4")
    src = tmp_path / "portrait.mov"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90",
                    "-i", str(flat), "-c", "copy", str(src)], check=True)
    assert _display_size(_video(_probe(src))) == (720, 1280)

    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    v = _video(_probe(out))
    assert (v["width"], v["height"]) == (540, 960)
    assert _display_size(v) == (540, 960)


@pytest.mark.fast
def test_video_without_audio_has_no_audio_track(tmp_path: Path) -> None:
    src = _make(tmp_path / "silent.mov", audio=0)
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    assert _audio(_probe(out)) == []


@pytest.mark.fast
def test_only_the_first_audio_track_is_kept(tmp_path: Path) -> None:
    src = _make(tmp_path / "two.mov", audio=2)
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    assert len(_audio(_probe(out))) == 1


@pytest.mark.fast
def test_high_frame_rates_are_capped_at_30(tmp_path: Path) -> None:
    src = _make(tmp_path / "fast.mov", rate=60)
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    num, den = (int(x) for x in _video(_probe(out))["avg_frame_rate"].split("/"))
    assert num / den <= 30.01
    assert float(_probe(out)["format"]["duration"]) == pytest.approx(2.0, abs=0.15)


@pytest.mark.fast
def test_small_videos_are_not_upscaled_and_stay_even(tmp_path: Path) -> None:
    src = _make(tmp_path / "small.mov", size="322x242")
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    v = _video(_probe(out))
    assert (v["width"], v["height"]) == (322, 242)


@pytest.mark.fast
def test_odd_scaled_sizes_round_to_even(tmp_path: Path) -> None:
    src = _make(tmp_path / "odd.mov", size="1000x562")
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    v = _video(_probe(out))
    assert v["width"] == 960 and v["height"] % 2 == 0


@pytest.mark.fast
def test_a_broken_file_raises_and_leaves_nothing(tmp_path: Path) -> None:
    src = tmp_path / "broken.mov"
    src.write_bytes(b"not a video at all")
    out = tmp_path / "out.mp4"
    with pytest.raises(RenderError):
        render_analysis_proxy(src, out)
    assert not out.exists()
    assert list(tmp_path.glob("out*")) == []


@pytest.mark.fast
def test_a_timeout_raises_and_leaves_nothing(tmp_path: Path) -> None:
    src = _make(tmp_path / "in.mov", seconds=4)
    out = tmp_path / "out.mp4"
    with pytest.raises(RenderError, match="timed out"):
        render_analysis_proxy(src, out, timeout=0.01)
    assert list(tmp_path.glob("out*")) == []


@pytest.mark.fast
def test_settings_shape_the_command() -> None:
    cmd = build_command(Path("/in.mov"), Path("/out.mp4"),
                        AnalysisProxySettings(max_edge=640, encoder="h264_nvenc"))
    joined = " ".join(cmd)
    assert "min(iw,640)" in joined and "min(ih,640)" in joined
    assert "h264_nvenc" in cmd
    assert "-movflags" in cmd and "+faststart" in cmd
    # Originals are only read: the source is an input, never an output.
    assert cmd[cmd.index("-i") + 1] == "/in.mov"
    assert cmd[-1] == "/out.mp4"


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

from src.client.proxy.analysis_cache import AnalysisProxyCache  # noqa: E402


class _Resp:
    def __init__(self, status: int, body: bytes = b"", fail_after: int | None = None) -> None:
        self.status_code = status
        self._body = body
        self._fail_after = fail_after

    def iter_bytes(self, chunk_size: int = 65536):
        sent = 0
        for i in range(0, len(self._body), 4):
            if self._fail_after is not None and sent >= self._fail_after:
                raise ConnectionError("connection dropped")
            chunk = self._body[i:i + 4]
            sent += len(chunk)
            yield chunk


class _Client:
    def __init__(self, responses: dict[str, _Resp]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    from contextlib import contextmanager

    @contextmanager
    def stream(self, path: str, **kwargs: object):
        self.calls.append(path)
        yield self.responses[path.split("/")[3]]


def _url(asset_id: str) -> str:
    return f"/v1/assets/{asset_id}/artifacts/analysis_proxy"


@pytest.mark.fast
def test_cache_downloads_once_then_reads_locally(tmp_path: Path) -> None:
    client = _Client({"ast_1": _Resp(200, b"proxy bytes")})
    cache = AnalysisProxyCache(client, cache_dir=tmp_path)
    first = cache.get("ast_1")
    assert first is not None and first.read_bytes() == b"proxy bytes"
    assert cache.get("ast_1") == first
    assert client.calls == [_url("ast_1")]


@pytest.mark.fast
def test_cache_returns_none_when_the_server_has_none(tmp_path: Path) -> None:
    cache = AnalysisProxyCache(_Client({"ast_1": _Resp(404)}), cache_dir=tmp_path)
    assert cache.get("ast_1") is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.fast
def test_a_dropped_download_leaves_no_partial_file(tmp_path: Path) -> None:
    client = _Client({"ast_1": _Resp(200, b"0123456789abcdef", fail_after=8)})
    cache = AnalysisProxyCache(client, cache_dir=tmp_path)
    assert cache.get("ast_1") is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.fast
def test_put_moves_a_rendered_file_in(tmp_path: Path) -> None:
    rendered = tmp_path / "work" / "render.mp4"
    rendered.parent.mkdir()
    rendered.write_bytes(b"fresh")
    cache = AnalysisProxyCache(_Client({}), cache_dir=tmp_path / "cache")
    path = cache.put("ast_1", rendered)
    assert path.read_bytes() == b"fresh"
    assert not rendered.exists()
    assert cache.get("ast_1") == path


@pytest.mark.fast
def test_cache_evicts_least_recently_used_beyond_its_size(tmp_path: Path) -> None:
    cache = AnalysisProxyCache(_Client({}), cache_dir=tmp_path / "cache", max_bytes=25)
    paths = {}
    for n, name in enumerate(["ast_a", "ast_b", "ast_c"]):
        src = tmp_path / f"{name}.mp4"
        src.write_bytes(b"x" * 10)
        paths[name] = cache.put(name, src)
        stamp = time.time() - 100 + n
        os.utime(paths[name], (stamp, stamp))
    # 30 bytes in a 25-byte cache: the oldest goes, the newest stays.
    cache.evict()
    assert not paths["ast_a"].exists()
    assert paths["ast_b"].exists() and paths["ast_c"].exists()


@pytest.mark.fast
def test_the_newest_file_survives_even_when_alone_too_big(tmp_path: Path) -> None:
    cache = AnalysisProxyCache(_Client({}), cache_dir=tmp_path / "cache", max_bytes=5)
    src = tmp_path / "big.mp4"
    src.write_bytes(b"x" * 50)
    path = cache.put("ast_big", src)
    assert path.exists()
