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
from src.processing.video.audio import MIX_HANDLER, AudioTrack, audio_tracks, speech_wav_command


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
    # The track on its own too (out: the mix, then track 1, then the late one).
    assert _mean_volume(out, 2, (0, 2)) < -80
    assert _mean_volume(out, 2, (3, 5)) > -40


@pytest.mark.fast
def test_with_several_tracks_every_audio_output_comes_from_the_mix_graph() -> None:
    # Mapping the tracks straight from the input beside a graph mixing them
    # (with the video in the file) stalls ffmpeg 9 now and then: about half
    # the renders of a late-starting lav under load sat idle until killed.
    tracks = [AudioTrack(0, 2, "aac"), AudioTrack(2, 1, "aac")]
    cmd = build_command(Path("/in.mov"), Path("/out.mp4"), tracks=tracks)
    audio_maps = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map" and cmd[i + 1] != "0:V:0"]
    assert audio_maps == ["[mix]", "[t0]", "[t1]"]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:a:0]aresample=async=1:first_pts=0,asplit=2[s0][t0]" in graph
    assert "[0:a:2]aresample=async=1:first_pts=0,asplit=2[s1][t1]" in graph
    # One track: no graph, mapped as it is.
    one = build_command(Path("/in.mov"), Path("/out.mp4"), tracks=[AudioTrack(1, 2, "aac")])
    assert "-filter_complex" not in one and "0:a:1" in one


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


def _with_undecodable_second_track(path: Path, tag: bytes = b"zzzz") -> Path:
    """Two tracks, the second relabelled `tag`: a codec ffmpeg doesn't know
    ("zzzz"), or iPhone spatial audio's ("apac"), which ffmpeg 9 knows as
    apple_apac and has no decoder for."""
    _make(path, layouts=["mono", "mono"], extra=["-c:a:0", "aac", "-c:a:1", "pcm_s16le"])
    data = path.read_bytes()
    assert data.count(b"sowt") == 1
    path.write_bytes(data.replace(b"sowt", tag))
    return path


@pytest.mark.fast
@pytest.mark.parametrize("tag", [b"zzzz", b"apac"], ids=["unknown", "iphone-spatial-audio"])
def test_an_undecodable_track_is_left_out_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    tag: bytes) -> None:
    src = _with_undecodable_second_track(tmp_path / "apac.mov", tag)
    assert [t.index for t in audio_tracks(src)] == [0]
    ran = _recording_ffmpeg(monkeypatch)
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out, AnalysisProxySettings(decoder="cpu"))
    assert [a["channels"] for a in _audio(_probe(out))] == [1]
    assert len(ran) == 1  # made on the first try, not after one that failed on the track


@pytest.mark.fast
def test_what_ffmpeg_decodes_is_asked_of_ffmpeg() -> None:
    from src.processing.video.audio import decodable_audio

    decodable = decodable_audio()
    assert decodable is not None and {"aac", "pcm_s16le", "mp3", "opus"} <= decodable
    assert "apple_apac" not in decodable  # known to ffmpeg 9, not decodable
    assert "h264" not in decodable  # audio only


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

    def flaky(source, dest, settings=None, tracks=(), hwaccel=None, pix_fmt=None):
        tried.append((len(tracks), hwaccel))
        cmd = real(source, dest, settings, tracks, hwaccel, pix_fmt)
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


def _keyframe_times(path: Path) -> list[float]:
    out = subprocess.run(["ffprobe", "-v", "error", "-skip_frame", "nokey", "-select_streams", "v:0",
                          "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path)],
                         check=True, capture_output=True, text=True).stdout
    return [float(t.strip(",")) for t in out.split() if t.strip(",")]


@pytest.mark.fast
@pytest.mark.parametrize("rate", ["24000/1001", "25", "30000/1001", "60"])
def test_a_keyframe_every_two_seconds_and_nowhere_else(tmp_path: Path, rate: str) -> None:
    # Scenes are found from the keyframes alone: a cut in the picture (at
    # 2.7 s here) mustn't get one of its own, and 24 fps footage mustn't get
    # them 2.5 s apart.
    src = tmp_path / "cut.mov"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={rate}:duration=2.7",
                    "-f", "lavfi", "-i", f"smptebars=size=640x360:rate={rate}:duration=4.3",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out)
    assert _keyframe_times(out) == pytest.approx([0.0, 2.0, 4.0, 6.0], abs=0.05)


@pytest.mark.fast
@pytest.mark.parametrize("encoder,own", [("libx264", ["-sc_threshold", "0"]),
                                         ("h264_nvenc", ["-no-scenecut", "1", "-forced-idr", "1"])])
def test_every_encoder_is_told_where_the_keyframes_go(encoder: str, own: list[str]) -> None:
    cmd = build_command(Path("/in.mov"), Path("/out.mp4"), AnalysisProxySettings(encoder=encoder))
    joined = " ".join(cmd)
    assert " ".join(own) in joined
    assert cmd[cmd.index("-force_key_frames") + 1] == "expr:gte(t,n_forced*2)"


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
def test_a_stuck_render_raises_and_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Stopped only when ffmpeg makes no progress (tests/test_ffmpeg_run.py), never for taking long.
    from src.processing.video import ffmpeg_run

    def stuck(cmd, *, stall_after):
        Path(cmd[-1]).write_bytes(b"half a proxy")
        raise ffmpeg_run.StalledError(f"ffmpeg made no progress for {stall_after:.0f} s")

    monkeypatch.setattr(ffmpeg_run, "run", stuck)
    src = _make(tmp_path / "in.mov")
    out = tmp_path / "out.mp4"
    with pytest.raises(RenderError, match="ffmpeg made no progress for 300 s on in.mov"):
        render_analysis_proxy(src, out)
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
from src.processing.video import ffmpeg_run  # noqa: E402

needs_vulkan = pytest.mark.skipif(not ffmpeg_run.vulkan_decodes(), reason="no Vulkan video decoding here")
needs_videotoolbox = pytest.mark.skipif(not ffmpeg_run.videotoolbox_decodes(), reason="no VideoToolbox here (not a Mac)")
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
    real = ffmpeg_run.run

    def run(cmd, **kwargs):
        if cmd[0] == "ffmpeg" and cmd[-1].endswith(".part"):
            ran.append(cmd)
        return real(cmd, **kwargs)

    monkeypatch.setattr(ffmpeg_run, "run", run)
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
    # Stopped at the first picture the hardware fails on, never filling the gap.
    assert cmd.index("-xerror") < cmd.index("-i")
    on_the_cpu = build_command(Path("/in.mov"), Path("/out.mp4"))
    assert "-hwaccel" not in on_the_cpu and "-xerror" not in on_the_cpu


@pytest.mark.fast
@pytest.mark.parametrize("choice,vulkan,videotoolbox,expected", [
    ("auto", True, False, "vulkan"),
    ("auto", False, True, "videotoolbox"),
    ("auto", False, False, None),
    ("cpu", True, True, None),
    ("", True, True, None),
    ("cuda", False, False, "cuda"),
    ("vulkan", False, False, "vulkan"),
])
def test_which_decoder(monkeypatch: pytest.MonkeyPatch, choice: str, vulkan: bool, videotoolbox: bool,
                       expected: str | None) -> None:
    monkeypatch.setattr(ffmpeg_run, "vulkan_decodes", lambda: vulkan)
    monkeypatch.setattr(ffmpeg_run, "videotoolbox_decodes", lambda: videotoolbox)
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


def _fake_renders(monkeypatch: pytest.MonkeyPatch, together: int = 1) -> tuple[list, list]:
    """_run records whether each render decoded on the GPU, and how many did at once:
    each is held until `together` have started, so they're at once however slow the machine."""
    import threading

    used: list[str | None] = []
    most = [0]
    now = [0]
    lock = threading.Lock()
    started = threading.Event()

    def run(source, part, settings, tracks, stall_after, hwaccel=None):
        with lock:
            used.append(hwaccel)
            if len(used) >= together:
                started.set()
            if hwaccel:
                now[0] += 1
                most[0] = max(most[0], now[0])
        assert started.wait(60), f"{len(used)} of {together} renders started"
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

    used, most = _fake_renders(monkeypatch, together=3)
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

    used, most = _fake_renders(monkeypatch, together=3)
    settings = AnalysisProxySettings(decoder="vulkan", gpu_decodes=slots)
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda i: render_analysis_proxy(tmp_path / f"{i}.mov", tmp_path / f"{i}.mp4", settings), range(3)))
    assert most[0] == slots and used.count("vulkan") == slots
    assert "gpu_decodes" not in settings.output()


@pytest.mark.fast
def test_a_macs_media_engine_takes_every_render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # It shares the machine's memory: no room on a GPU to keep for the models,
    # so no turns, and every render decodes there.
    from concurrent.futures import ThreadPoolExecutor

    used, most = _fake_renders(monkeypatch, together=3)
    monkeypatch.setattr(AP, "_gpu_has_room", lambda: False)  # no GPU's memory to ask about
    settings = AnalysisProxySettings(decoder="videotoolbox", gpu_decodes=1)
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(lambda i: render_analysis_proxy(tmp_path / f"{i}.mov", tmp_path / f"{i}.mp4", settings), range(3)))
    assert used == ["videotoolbox"] * 3
    # gpu_decodes 0 still means none on the hardware.
    render_analysis_proxy(tmp_path / "x.mov", tmp_path / "x.mp4", AnalysisProxySettings(decoder="videotoolbox",
                                                                                         gpu_decodes=0))
    assert used[-1] is None


@pytest.mark.fast
def test_without_room_on_the_gpu_the_cpu_decodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    used, _ = _fake_renders(monkeypatch)
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
    # 10-bit HEVC turned on its side, like prod's camera and phone files. A
    # second of 720p: decoding it fits beside whatever holds the GPU's memory
    # (an AI model). The room check is the renderer's policy, tested on its own
    # (test_room_on_the_gpu_is_its_free_memory), not a reason to skip this one.
    flat = _make(tmp_path / "flat.mp4", seconds=1.0,
                 extra=["-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-x265-params", "log-level=error"])
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


@needs_videotoolbox
@pytest.mark.fast
def test_a_macs_media_engine_makes_the_same_proxy_as_the_cpu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 10-bit HEVC turned on its side, like an iPhone's portrait video.
    flat = _make(tmp_path / "flat.mp4", extra=["-c:v", "libx265", "-pix_fmt", "yuv420p10le",
                                               "-x265-params", "log-level=error"])
    src = tmp_path / "portrait.mov"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90",
                    "-i", str(flat), "-c", "copy", str(src)], check=True)
    ran = _recording_ffmpeg(monkeypatch)

    engine, cpu = tmp_path / "engine.mp4", tmp_path / "cpu.mp4"
    render_analysis_proxy(src, engine, AnalysisProxySettings(decoder="auto"))
    render_analysis_proxy(src, cpu, AnalysisProxySettings(decoder="cpu"))

    assert [cmd[cmd.index("-hwaccel") + 1] if "-hwaccel" in cmd else None for cmd in ran] == ["videotoolbox", None]
    assert (_video(_probe(engine))["width"], _video(_probe(engine))["height"]) == (540, 960)
    assert _frame_hashes(engine) == _frame_hashes(cpu)


def _damaged(path: Path) -> Path:
    """Three seconds of H.264 with three runs of its pictures' bytes garbled.
    The CPU's decoder goes on past them (90 pictures); a Mac's media engine
    fails on some and ffmpeg, left to itself, leaves those out (69) and
    still finishes, as it does on a Sony 4:2:2 10-bit H.264 clip."""
    ok = _make(path.with_name("ok.mp4"), seconds=3.0, audio=0, extra=["-g", "30", "-movflags", "+faststart"])
    data = bytearray(ok.read_bytes())
    start = data.find(b"mdat") + 8
    for share in (0.45, 0.62, 0.8):
        at = start + int((len(data) - start) * share)
        data[at:at + 64] = bytes(b ^ 0x5A for b in data[at:at + 64])
    path.write_bytes(bytes(data))
    return path


@needs_videotoolbox
@pytest.mark.fast
def test_a_picture_the_hardware_cant_decode_sends_the_render_to_the_cpu(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _damaged(tmp_path / "damaged.mp4")
    ran = _recording_ffmpeg(monkeypatch)

    out = tmp_path / "out.mp4"
    render_analysis_proxy(src, out, AnalysisProxySettings(decoder="videotoolbox"))

    # The media engine's try stopped at its first failed picture; the CPU made
    # it, every picture. Not compared with another CPU render: ffmpeg's threaded
    # decoder hides a damaged picture differently from one run to the next.
    assert [cmd[cmd.index("-hwaccel") + 1] if "-hwaccel" in cmd else None for cmd in ran] == [
        "videotoolbox", None]
    assert len(_frame_hashes(out)) == 90


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


def _cached(cache: AnalysisProxyCache, asset_id: str) -> Path | None:
    """Where the cache has it, as a job holds it (and lets go)."""
    with cache.held(asset_id) as path:
        return path


@pytest.mark.fast
def test_cache_downloads_once_then_reads_locally(tmp_path: Path) -> None:
    client = _Client({"ast_1": _Resp(200, b"proxy bytes")})
    cache = AnalysisProxyCache(client, cache_dir=tmp_path)
    with cache.held("ast_1") as first:
        assert first is not None and first.read_bytes() == b"proxy bytes"
    assert _cached(cache, "ast_1") == first
    assert client.calls == [_url("ast_1")]


@pytest.mark.fast
def test_cache_returns_none_when_the_server_has_none(tmp_path: Path) -> None:
    cache = AnalysisProxyCache(_Client({"ast_1": _Resp(404)}), cache_dir=tmp_path)
    assert _cached(cache, "ast_1") is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.fast
def test_a_dropped_download_leaves_no_partial_file(tmp_path: Path) -> None:
    client = _Client({"ast_1": _Resp(200, b"0123456789abcdef", fail_after=8)})
    cache = AnalysisProxyCache(client, cache_dir=tmp_path)
    assert _cached(cache, "ast_1") is None
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
    assert _cached(cache, "ast_1") == path


def _put(cache: AnalysisProxyCache, tmp_path: Path, asset_id: str, size: int, age: float = 0.0) -> Path:
    src = tmp_path / f"{asset_id}.render"
    src.write_bytes(b"x" * size)
    path = cache.put(asset_id, src)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.fast
def test_cache_evicts_least_recently_used_beyond_its_size(tmp_path: Path) -> None:
    cache = AnalysisProxyCache(_Client({}), cache_dir=tmp_path / "cache", max_bytes=25)
    a, b = _put(cache, tmp_path, "ast_a", 10, age=100), _put(cache, tmp_path, "ast_b", 10, age=99)
    c = _put(cache, tmp_path, "ast_c", 10)
    # 30 bytes in a 25-byte cache: the oldest goes, the newest stays.
    assert not a.exists()
    assert b.exists() and c.exists()


@pytest.mark.fast
def test_the_newest_file_survives_even_when_alone_too_big(tmp_path: Path) -> None:
    cache = AnalysisProxyCache(_Client({}), cache_dir=tmp_path / "cache", max_bytes=5)
    assert _put(cache, tmp_path, "ast_big", 50).exists()


@pytest.mark.fast
def test_a_proxy_a_job_holds_is_never_evicted_the_next_oldest_goes_instead(tmp_path: Path) -> None:
    """A transcript reads it by path more than once (ffprobe, then ffmpeg)."""
    cache = AnalysisProxyCache(_Client({}), cache_dir=tmp_path / "cache", max_bytes=25)
    a = _put(cache, tmp_path, "ast_a", 10, age=100)
    with cache.held("ast_a") as held:
        b, c = _put(cache, tmp_path, "ast_b", 10, age=50), _put(cache, tmp_path, "ast_c", 10)
        assert held == a and a.exists()
        assert not b.exists() and c.exists()


@pytest.mark.fast
def test_with_nothing_else_to_go_the_cache_is_over_its_cap_until_let_go(tmp_path: Path) -> None:
    cache = AnalysisProxyCache(_Client({}), cache_dir=tmp_path / "cache", max_bytes=15)
    a = _put(cache, tmp_path, "ast_a", 10, age=100)
    with cache.held("ast_a"):
        c = _put(cache, tmp_path, "ast_c", 10)
        assert a.exists() and c.exists(), "20 bytes in a 15-byte cache, rather than the work failing"
    d = _put(cache, tmp_path, "ast_d", 1)
    assert not a.exists() and c.exists() and d.exists(), "let go of: it goes at the next write"


@pytest.mark.fast
def test_a_download_is_held_from_before_it_starts(tmp_path: Path) -> None:
    """Fetched and then read: nothing evicts it in between."""
    cache = AnalysisProxyCache(_Client({"ast_new": _Resp(200, b"y" * 20)}), cache_dir=tmp_path / "cache",
                               max_bytes=25)
    _put(cache, tmp_path, "ast_old", 10, age=10)
    with cache.held("ast_new") as path:
        os.utime(path, (time.time() - 1000,) * 2)  # older than anything: first to go, were it not held
        _put(cache, tmp_path, "ast_other", 10)
        assert path.read_bytes() == b"y" * 20


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
