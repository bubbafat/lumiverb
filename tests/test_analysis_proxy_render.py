"""Rendering analysis proxies with ffmpeg, and the local cache (ADR-016 phase 2).

An analysis proxy is a full-length, low-resolution copy of a video with all
its audio tracks, each at most stereo and at a low bitrate. With several
tracks, a mix of them comes first: browsers play only the first track, and a
lav may be on any of them. Transcription, scenes and vision read it instead of the
original. These tests render real files made with ffmpeg's test sources.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from src.processing.video.analysis_proxy import (
    AnalysisProxySettings,
    RenderError,
    build_command,
    render_analysis_proxy,
)
from src.processing.video.audio import MIX_HANDLER, audio_tracks, speech_wav_command


def _make(path: Path, *, size: str = "1280x720", rate: int = 30, seconds: float = 2.0,
          audio: int = 1, extra: list[str] | None = None, layouts: list[str] | None = None,
          sample_rate: int = 48000, silent: tuple[int, ...] = ()) -> Path:
    """A test video; `layouts` gives each audio track's channel layout, `silent` the silent ones."""
    layouts = layouts or ["mono"] * audio
    audio = len(layouts)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}"]
    for i, layout in enumerate(layouts):
        tone = "volume=0" if i in silent else "volume=1"
        cmd += ["-f", "lavfi", "-i",
                f"sine=frequency={440 + 220 * i}:sample_rate={sample_rate}:duration={seconds},{tone},"
                f"aformat=channel_layouts={layout}"]
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


def _handlers(info: dict) -> list[str]:
    return [a.get("tags", {}).get("handler_name", "") for a in _audio(info)]


def _mean_volume(path: Path, track: int, between: tuple[float, float] | None = None) -> float:
    """Mean dB of an audio track, or of its seconds `between` (by timestamp)."""
    trim = f"atrim={between[0]}:{between[1]}," if between else ""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-map", f"0:a:{track}",
                          "-af", f"{trim}volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    return float(out.split("mean_volume:")[1].split("dB")[0])


def _late_lav(path: Path, *, gap: bool = False) -> Path:
    """6 s, track 1 silent throughout, track 2 a tone only from 3 s to 5 s.

    The tone's track starts late (start_time near 3 s), or with `gap`, starts
    at 0 with a hole in its timestamps from 1 s to 3 s and the tone after it.
    """
    tone = path.with_name("tone.mkv")
    source = "sine=frequency=660:sample_rate=48000:duration="
    source += "3,volume=0:enable='lt(t,1)'" if gap else "2"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", source,
                    "-c:a", "aac", str(tone)], check=True)
    late = ["-c:a:1", "copy", "-bsf:a:1", r"setts=ts=TS+if(gte(TS*TB\,1)\,2/TB\,0)"] if gap else []
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=6",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6,volume=0",
           *([] if gap else ["-itsoffset", "3"]), "-i", str(tone),
           "-map", "0:v", "-map", "1:a", "-map", "2:a",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", *late, str(path)]
    subprocess.run(cmd, check=True)
    return path


@pytest.mark.fast
def test_one_track_stays_as_it_is(tmp_path: Path) -> None:
    src = _make(tmp_path / "one.mov", layouts=["stereo"])
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    info = _probe(out)
    assert [a["channels"] for a in _audio(info)] == [2]
    assert MIX_HANDLER not in _handlers(info)


@pytest.mark.fast
def test_several_tracks_get_a_mix_first_then_every_track_in_order(tmp_path: Path) -> None:
    # Pro cameras put a lav on its own track; browsers play only the first.
    src = _make(tmp_path / "three.mov", layouts=["mono", "stereo", "5.1"])
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    info = _probe(out)
    assert [a["channels"] for a in _audio(info)] == [2, 1, 2, 2]
    assert _handlers(info)[0] == MIX_HANDLER
    assert MIX_HANDLER not in _handlers(info)[1:]


@pytest.mark.fast
def test_the_mix_keeps_a_lone_voice_as_loud_as_it_was(tmp_path: Path) -> None:
    # amix's default divides by the number of tracks: one live track of eight
    # would come out 18 dB down.
    src = _make(tmp_path / "eight.mov", layouts=["mono"] * 8, silent=(0, 1, 2, 3, 5, 6, 7), seconds=3)
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    live = _mean_volume(src, 4)
    mix = _mean_volume(out, 0)
    assert abs(mix - live) < 4, (live, mix)


@pytest.mark.fast
@pytest.mark.parametrize("gap", [False, True], ids=["starts-late", "has-a-gap"])
def test_the_mix_keeps_each_track_at_its_time(tmp_path: Path, gap: bool) -> None:
    # amix takes samples in order: a lav that starts 3 s in would play from 0
    # in the mix, out of step with the picture and the transcript.
    src = _late_lav(tmp_path / "late.mkv", gap=gap)
    assert _mean_volume(src, 1, (3, 5)) > -40
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    assert _mean_volume(out, 0, (0, 2)) < -80
    assert _mean_volume(out, 0, (3, 5)) > -40


@pytest.mark.fast
def test_audio_is_low_bitrate_at_48k(tmp_path: Path) -> None:
    src = _make(tmp_path / "hi.mov", layouts=["mono", "stereo"], sample_rate=96000, seconds=4,
                extra=["-c:a", "pcm_s32le"])
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    mix, mono, stereo = _audio(_probe(out))
    assert {t["codec_name"] for t in (mix, mono, stereo)} == {"aac"}
    assert {t["sample_rate"] for t in (mix, mono, stereo)} == {"48000"}
    assert int(mono["bit_rate"]) <= 60_000
    assert int(stereo["bit_rate"]) <= 110_000
    assert int(mix["bit_rate"]) <= 110_000


def _with_undecodable_second_track(path: Path) -> Path:
    """Two tracks, the second relabelled with a codec no ffmpeg decodes (as iPhone spatial audio's apac)."""
    _make(path, layouts=["mono", "mono"], extra=["-c:a:0", "aac", "-c:a:1", "pcm_s16le"])
    data = path.read_bytes()
    assert data.count(b"sowt") == 1
    path.write_bytes(data.replace(b"sowt", b"zzzz"))
    return path


@pytest.mark.fast
def test_an_undecodable_track_is_left_out_not_fatal(tmp_path: Path) -> None:
    src = _with_undecodable_second_track(tmp_path / "apac.mov")
    assert [t.index for t in audio_tracks(src)] == [0]
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    assert [a["channels"] for a in _audio(_probe(out))] == [1]


@pytest.mark.fast
@pytest.mark.parametrize("decoder,expected", [
    ("cpu", [(2, None), (1, None)]),
    # The GPU, then the CPU, with every track; then both with the first.
    ("no-such-hwaccel", [(2, "no-such-hwaccel"), (2, None), (1, "no-such-hwaccel"), (1, None)]),
])
def test_a_render_that_fails_on_several_tracks_retries_with_the_first(
        tmp_path: Path, monkeypatch, decoder: str, expected: list) -> None:
    from src.processing.video import analysis_proxy

    src = _make(tmp_path / "two.mov", layouts=["mono", "mono"])
    real = analysis_proxy.build_command
    tried: list[tuple[int, str | None]] = []

    def flaky(source, dest, settings=None, tracks=(), hwaccel=None):
        tried.append((len(tracks), hwaccel))
        cmd = real(source, dest, settings, tracks, hwaccel)
        return cmd if len(tracks) == 1 else [*cmd[:-1], "-c:a", "no_such_encoder", cmd[-1]]

    monkeypatch.setattr(analysis_proxy, "build_command", flaky)
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out, AnalysisProxySettings(decoder=decoder))
    assert tried == expected
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
    # The first real video stream: never cover art (V, not v).
    assert cmd[cmd.index("-map") + 1] == "0:V:0"
    # Originals are only read: the source is an input, never an output.
    assert cmd[cmd.index("-i") + 1] == "/in.mov"
    assert cmd[-1] == "/out.mp4"


# ---------------------------------------------------------------------------
# Decoding on the GPU
# ---------------------------------------------------------------------------

from src.processing.video import analysis_proxy as AP  # noqa: E402

needs_vulkan = pytest.mark.skipif(not AP._vulkan_decodes(), reason="no Vulkan video decoding here")
_real_gpu_has_room = AP._gpu_has_room


@pytest.fixture(autouse=True)
def _room_on_the_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Renders here never depend on how full this machine's GPU is right now."""
    monkeypatch.setattr(AP, "_gpu_has_room", lambda: True)


def _frame_hashes(path: Path) -> list[str]:
    """Each picture's hash, to compare two proxies frame by frame."""
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
                          "-map", "0:v:0", "-f", "framemd5", "-"], check=True, capture_output=True, text=True).stdout
    return [line.rsplit(",", 1)[1].strip() for line in out.splitlines() if not line.startswith("#")]


def _recording_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The ffmpeg commands renders run (into a .part), still run for real."""
    ran: list[list[str]] = []
    real = AP.subprocess.run

    def run(cmd, *args, **kwargs):
        if cmd[0] == "ffmpeg" and cmd[-1].endswith(".part"):
            ran.append(cmd)
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(AP.subprocess, "run", run)
    return ran


@pytest.mark.fast
def test_the_decoder_is_this_machines_and_doesnt_change_what_is_made() -> None:
    # H.264 and HEVC decoding is exact, so where it happens is a machine
    # choice like the encoder: never tracked, never set by the server.
    s = AnalysisProxySettings.for_producer({"max_edge": 640, "decoder": "cpu", "gpu_decodes": 9}, "libx264", "cuda", 2)
    assert (s.max_edge, s.encoder, s.decoder, s.gpu_decodes) == (640, "libx264", "cuda", 2)
    assert "decoder" not in s.output() and "encoder" not in s.output()
    assert AnalysisProxySettings.for_producer({"decoder": "cpu"}, "libx264").decoder == "auto"


@pytest.mark.fast
def test_gpu_decoding_is_asked_for_before_the_input() -> None:
    cmd = build_command(Path("/in.mov"), Path("/out.mp4"), hwaccel="vulkan")
    assert cmd[cmd.index("-hwaccel") + 1] == "vulkan"
    assert cmd.index("-hwaccel") < cmd.index("-i")
    assert "-hwaccel" not in build_command(Path("/in.mov"), Path("/out.mp4"))


@pytest.mark.fast
@pytest.mark.parametrize("choice,vulkan,expected", [
    ("auto", True, "vulkan"),
    ("auto", False, None),
    ("cpu", True, None),
    ("", True, None),
    ("cuda", False, "cuda"),
    ("vulkan", False, "vulkan"),
])
def test_which_decoder(monkeypatch: pytest.MonkeyPatch, choice: str, vulkan: bool, expected: str | None) -> None:
    monkeypatch.setattr(AP, "_vulkan_decodes", lambda: vulkan)
    assert AP.gpu_decoder(choice) == expected


@pytest.mark.fast
def test_when_gpu_decoding_fails_the_cpu_renders_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A missing device or driver, or a stream the GPU gives up on halfway.
    ran = _recording_ffmpeg(monkeypatch)
    src = _make(tmp_path / "in.mov")
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out, AnalysisProxySettings(decoder="no-such-hwaccel"))

    info = _probe(out)
    assert (_video(info)["width"], _video(info)["height"]) == (960, 540)
    assert len(_audio(info)) == 1
    assert ["-hwaccel" in cmd for cmd in ran] == [True, False]
    assert list(tmp_path.glob("out.mp4.*")) == []


@pytest.mark.fast
def test_a_broken_file_fails_on_the_cpu_too_and_leaves_nothing(tmp_path: Path) -> None:
    src = tmp_path / "broken.mov"
    src.write_bytes(b"not a video at all")
    out = tmp_path / "out.mp4"
    with pytest.raises(RenderError):
        render_analysis_proxy(src, out, AnalysisProxySettings(decoder="no-such-hwaccel"))
    assert list(tmp_path.glob("out*")) == []


@pytest.mark.fast
def test_decoding_on_the_cpu_never_asks_for_the_gpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _recording_ffmpeg(monkeypatch)
    render_analysis_proxy(_make(tmp_path / "in.mov"), tmp_path / "out.mp4", AnalysisProxySettings(decoder="cpu"))
    assert ran and not any("-hwaccel" in cmd for cmd in ran)


def _fake_renders(monkeypatch: pytest.MonkeyPatch, seconds: float = 0.3) -> tuple[list, list]:
    """_run records whether each render decoded on the GPU, and how many did at once."""
    import threading

    used: list[str | None] = []
    most = [0]
    now = [0]
    lock = threading.Lock()

    def run(source, part, settings, tracks, timeout, hwaccel=None):
        with lock:
            used.append(hwaccel)
            if hwaccel:
                now[0] += 1
                most[0] = max(most[0], now[0])
        time.sleep(seconds)
        with lock:
            if hwaccel:
                now[0] -= 1
        part.write_bytes(b"proxy")

    monkeypatch.setattr(AP, "audio_tracks", lambda source: [])
    monkeypatch.setattr(AP, "_run", run)
    return used, most


@pytest.mark.fast
def test_one_gpu_decode_at_a_time_and_the_rest_on_the_cpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # One saturates the 3080's video decoder, and each holds video memory the
    # vision model and Whisper need: the others decode on the CPU meanwhile.
    from concurrent.futures import ThreadPoolExecutor

    used, most = _fake_renders(monkeypatch)
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda i: render_analysis_proxy(tmp_path / f"{i}.mov", tmp_path / f"{i}.mp4",
                                                      AnalysisProxySettings(decoder="vulkan")), range(3)))
    assert most[0] == 1
    assert used.count("vulkan") == 1 and used.count(None) == 2
    # Free again afterwards.
    render_analysis_proxy(tmp_path / "x.mov", tmp_path / "x.mp4", AnalysisProxySettings(decoder="vulkan"))
    assert used[-1] == "vulkan"


@pytest.mark.fast
@pytest.mark.parametrize("slots", [2, 0])
def test_how_many_decode_on_the_gpu_at_once_is_this_machines_choice(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slots: int) -> None:
    from concurrent.futures import ThreadPoolExecutor

    used, most = _fake_renders(monkeypatch)
    settings = AnalysisProxySettings(decoder="vulkan", gpu_decodes=slots)
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda i: render_analysis_proxy(tmp_path / f"{i}.mov", tmp_path / f"{i}.mp4", settings), range(3)))
    assert most[0] == slots and used.count("vulkan") == slots
    assert "gpu_decodes" not in settings.output()


@pytest.mark.fast
def test_without_room_on_the_gpu_the_cpu_decodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    used, _ = _fake_renders(monkeypatch, seconds=0)
    monkeypatch.setattr(AP, "_gpu_has_room", lambda: False)
    render_analysis_proxy(tmp_path / "a.mov", tmp_path / "a.mp4", AnalysisProxySettings(decoder="vulkan"))
    assert used == [None]
    # The GPU's turn isn't kept by a render that didn't use it.
    monkeypatch.setattr(AP, "_gpu_has_room", lambda: True)
    render_analysis_proxy(tmp_path / "b.mov", tmp_path / "b.mp4", AnalysisProxySettings(decoder="vulkan"))
    assert used == [None, "vulkan"]


@pytest.mark.fast
@pytest.mark.parametrize("answer,room", [
    ((0, "4096\n"), True),
    ((0, "900\n"), False),
    ((0, "900\n6000\n"), False),  # the first GPU is the one ffmpeg picks
    ((9, ""), True),  # nvidia-smi failed: nothing to go by, try
    ((0, "[N/A]\n"), True),
    (FileNotFoundError(), True),  # not NVIDIA: nothing to go by, try
])
def test_room_on_the_gpu_is_its_free_memory(monkeypatch: pytest.MonkeyPatch, answer, room: bool) -> None:
    import types

    def run(cmd, *args, **kwargs):
        assert cmd[0] == "nvidia-smi"
        if isinstance(answer, Exception):
            raise answer
        return types.SimpleNamespace(returncode=answer[0], stdout=answer[1])

    monkeypatch.setattr(AP.subprocess, "run", run)
    assert _real_gpu_has_room() is room


@needs_vulkan
@pytest.mark.fast
def test_the_gpu_makes_the_same_proxy_as_the_cpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 10-bit HEVC turned on its side, like the brain's camera and phone files.
    if not _real_gpu_has_room():
        pytest.skip("the GPU is too full right now to decode beside what's on it")
    flat = _make(tmp_path / "flat.mp4", extra=["-c:v", "libx265", "-pix_fmt", "yuv420p10le",
                                               "-x265-params", "log-level=error"])
    src = tmp_path / "portrait.mov"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90",
                    "-i", str(flat), "-c", "copy", str(src)], check=True)
    ran = _recording_ffmpeg(monkeypatch)

    gpu, cpu = tmp_path / "gpu.mp4", tmp_path / "cpu.mp4"
    render_analysis_proxy(src, gpu, AnalysisProxySettings(decoder="vulkan"))
    render_analysis_proxy(src, cpu, AnalysisProxySettings(decoder="cpu"))

    assert [cmd[cmd.index("-hwaccel") + 1] if "-hwaccel" in cmd else None for cmd in ran] == ["vulkan", None]
    assert (_video(_probe(gpu))["width"], _video(_probe(gpu))["height"]) == (540, 960)
    assert _frame_hashes(gpu) == _frame_hashes(cpu)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

from src.processing.proxy.analysis_cache import AnalysisProxyCache  # noqa: E402


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


# ---------------------------------------------------------------------------
# Audio for transcription
# ---------------------------------------------------------------------------



def _peak(wav: Path) -> int:
    import array
    import wave

    with wave.open(str(wav)) as w:
        samples = array.array("h", w.readframes(w.getnframes()))
    return max((abs(x) for x in samples), default=0)


@pytest.mark.fast
def test_audio_tracks_lists_each_track_in_order(tmp_path: Path) -> None:
    tracks = audio_tracks(_make(tmp_path / "a.mov", layouts=["mono", "stereo", "5.1"]))
    assert [(t.index, t.channels) for t in tracks] == [(0, 1), (1, 2), (2, 6)]
    assert audio_tracks(_make(tmp_path / "s.mov", audio=0)) == []


@pytest.mark.fast
def test_audio_tracks_of_an_unreadable_file_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mov"
    bad.write_bytes(b"nope")
    with pytest.raises(subprocess.CalledProcessError):
        audio_tracks(bad)


@pytest.mark.fast
@pytest.mark.parametrize("layouts,silent", [
    (["stereo", "mono"], (0,)),   # camera mic silent, lav on track 2
    (["mono", "mono"], (1,)),     # lav on track 1, track 2 silent
    (["mono"], ()),               # one track
])
def test_speech_audio_hears_every_track(tmp_path: Path, layouts: list[str], silent: tuple[int, ...]) -> None:
    src = _make(tmp_path / "clip.mov", layouts=layouts, silent=silent)
    wav = tmp_path / "speech.wav"
    subprocess.run(speech_wav_command(src, wav, audio_tracks(src)), check=True)
    import wave

    with wave.open(str(wav)) as w:
        assert (w.getframerate(), w.getnchannels()) == (16000, 1)
    assert _peak(wav) > 1000


@pytest.mark.fast
def test_speech_from_a_proxy_uses_its_mix(tmp_path: Path) -> None:
    src = _make(tmp_path / "clip.mov", layouts=["stereo", "mono"], silent=(0,))
    proxy = tmp_path / "proxy.mp4"
    render_analysis_proxy(src, proxy)
    tracks = audio_tracks(proxy)
    assert tracks[0].handler == MIX_HANDLER
    cmd = speech_wav_command(proxy, tmp_path / "s.wav", tracks)
    assert "-filter_complex" not in cmd and cmd[cmd.index("-map") + 1] == "0:a:0"
    subprocess.run(cmd, check=True)
    assert _peak(tmp_path / "s.wav") > 1000


@pytest.mark.fast
@pytest.mark.parametrize("alone", [False, True], ids=["mixed", "one-track"])
def test_speech_audio_keeps_each_track_at_its_time(tmp_path: Path, alone: bool) -> None:
    # Whisper's timestamps are times in the clip: a late lav must stay late.
    src = _late_lav(tmp_path / "late.mkv")
    if alone:
        one = tmp_path / "one.mkv"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        "-map", "0:v", "-map", "0:a:1", "-c", "copy", str(one)], check=True)
        src = one
    wav = tmp_path / "s.wav"
    subprocess.run(speech_wav_command(src, wav, audio_tracks(src)), check=True)
    assert _mean_volume(wav, 0, (0, 2)) < -80
    assert _mean_volume(wav, 0, (3, 5)) > -40


@pytest.mark.fast
def test_speech_mixing_doesnt_quieten_a_lone_voice(tmp_path: Path) -> None:
    src = _make(tmp_path / "eight.mov", layouts=["mono"] * 8, silent=(0, 1, 2, 3, 5, 6, 7), seconds=3)
    wav = tmp_path / "s.wav"
    subprocess.run(speech_wav_command(src, wav, audio_tracks(src)), check=True)
    assert abs(_mean_volume(wav, 0) - _mean_volume(src, 4)) < 4
