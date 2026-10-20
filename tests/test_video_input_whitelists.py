"""Every ffmpeg and ffprobe that opens a video opens a local file only, read
only as one of the containers Lumiverb takes in (src/processing/video/
ffmpeg_run.py input_args, derived from src/shared/file_extensions.py).

Each production reader is run for real. On a real clip it reads it, and
every ffmpeg and ffprobe it runs has the whitelists before the clip. On a
file named like a clip whose content is ffmpeg's concat format (a list of
other files), or an HLS playlist, it reads nothing, though what it names is
that same real clip beside it."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from src.processing import ingest
from src.processing.video import analysis_proxy, clip_extractor, ffmpeg_run
from src.processing.video.audio import AudioTrack, audio_tracks, speech_wav_command
from src.processing.video.probe import probe_video
from src.processing.video.video_scanner import VideoScanner
from src.shared.file_extensions import VIDEO_EXTENSIONS, VIDEO_EXTENSIONS_LIST

pytestmark = pytest.mark.fast


def _clip(path: Path, seconds: float = 2.0) -> Path:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15",
                    "-c:a", "aac", str(path)], check=True)
    return path


# Files named like clips whose content names the real clip beside them.
NOT_CLIPS = {
    "a list of files named .mov": ("listed.mov", "ffconcat version 1.0\nfile real.mp4\nduration 2.0\n"),
    "a list of files named .mp4": ("listed.mp4", "ffconcat version 1.0\nfile real.mp4\nduration 2.0\n"),
    # A recent ffmpeg (9 here) won't take a playlist by a name but .m3u8 or
    # .m3u on its own; an older one may.
    "a playlist named .mp4": ("playlist.mp4",
                              "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nreal.mp4\n#EXT-X-ENDLIST\n"),
}


@pytest.fixture(scope="module")
def real(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("clips")
    for name, content in NOT_CLIPS.values():
        (folder / name).write_text(content)
    return _clip(folder / "real.mp4")


def _playback_cut(path: Path, tmp: Path) -> bool:
    """The API's capped playback: probed, then cut to its first second. The
    file is kept in a Store over its own folder (the key is its name), so
    what's opened is the file itself."""
    from src.server.api.routers import playback
    from src.server.storage.cache import playback_cuts
    from src.server.storage.files import FileStore
    from src.server.storage.local import LocalStorage

    store = FileStore(LocalStorage(str(path.parent)))
    cuts = playback_cuts("ten_1", root=tmp / "data")
    ask = playback._Ask(store, path.name, 1, cuts, "ast_01M4CWVZ2MPTAV6FSGG6B6VXVP",
                        "lumiverb.video.analysis_proxy", path.stem, strip=False)
    playback.start_cuts()
    playback._make_cut(ask)
    return cuts.exists(playback._cut_for(ask)[0])


def _playback_duration(path: Path, tmp: Path) -> float | None:
    from src.server.api.routers import playback

    return playback._duration(path)


# Every reader: what it returns is truthy when it read the file.
READERS: dict[str, Callable[[Path, Path], object]] = {
    "probe_video": lambda p, t: probe_video(p).duration_sec,
    "audio_tracks": lambda p, t: audio_tracks(p),
    "source_pix_fmt": lambda p, t: analysis_proxy.source_pix_fmt(p),
    "render_analysis_proxy": lambda p, t: analysis_proxy.render_analysis_proxy(p, t / "proxy.mp4")
    or (t / "proxy.mp4").stat().st_size,
    "speech_wav_command": lambda p, t: subprocess.run(
        speech_wav_command(p, t / "speech.wav", [AudioTrack(0, 1, "aac")]), capture_output=True).returncode == 0,
    "scan: video dimensions": lambda p, t: ingest._probe_video_dimensions(p),
    "scan: preview": lambda p, t: ingest._generate_video_preview(p),
    "scan: poster": lambda p, t: ingest._extract_video_poster(p),
    "extract_video_frame": lambda p, t: clip_extractor.extract_video_frame_detailed(p, t / "frame.jpg", 0.5).ok,
    "scenes: VideoScanner": lambda p, t: list(VideoScanner(p).scan(0.0, 10.0)),
    "playback: duration": _playback_duration,
    "playback: cut": _playback_cut,
}


def _reads(reader: Callable[[Path, Path], object], path: Path, tmp: Path) -> bool:
    """Whether `reader` read `path`. Failing any way at all is not reading it:
    the same reader reads the real clip, so its failure is the file's."""
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        return bool(reader(path, tmp))
    except Exception:  # noqa: BLE001
        return False


def _recording(monkeypatch) -> list[list[str]]:
    """Every command run from here on (subprocess.run, ffmpeg_run.run and
    asyncio all start them with subprocess.Popen)."""
    ran: list[list[str]] = []
    real_popen = subprocess.Popen

    class Recorded(real_popen):
        def __init__(self, args, *a, **k):
            ran.append([str(arg) for arg in args])
            super().__init__(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", Recorded)
    return ran


def _whitelisted_before(cmd: list[str], path: Path) -> bool:
    """Whether input_args come before `path` in `cmd` (ffmpeg: before its -i; ffprobe: before the file)."""
    before = cmd[:cmd.index(str(path))]
    args = ffmpeg_run.input_args()
    return any(before[i:i + len(args)] == args for i in range(len(before)))


@pytest.mark.parametrize("reader", list(READERS.values()), ids=list(READERS))
def test_each_reader_reads_a_real_clip_with_the_whitelists(reader, real, tmp_path, monkeypatch):
    ran = _recording(monkeypatch)
    assert _reads(reader, real, tmp_path)
    opened = [cmd for cmd in ran if Path(cmd[0]).name in ("ffmpeg", "ffprobe") and str(real) in cmd]
    assert opened, "it ran ffmpeg or ffprobe on the clip"
    for cmd in opened:
        assert _whitelisted_before(cmd, real), cmd


@pytest.mark.parametrize("not_clip", NOT_CLIPS.keys())
@pytest.mark.parametrize("reader", list(READERS.values()), ids=list(READERS))
def test_each_reader_refuses_a_file_naming_another(reader, not_clip, real, tmp_path):
    path = real.parent / NOT_CLIPS[not_clip][0]
    assert not _reads(reader, path, tmp_path)


def test_a_source_named_by_another_protocol_isnt_opened(real, tmp_path):
    # Only the file protocol: "file:" opens the clip; ffmpeg's concat protocol over the same clip doesn't.
    assert probe_video(Path(f"file:{real}")).duration_sec
    with pytest.raises(subprocess.CalledProcessError):
        probe_video(Path(f"concat:{real}"))
    with pytest.raises(analysis_proxy.RenderError):
        analysis_proxy.render_analysis_proxy(Path(f"concat:{real}"), tmp_path / "proxy.mp4")
    assert not (tmp_path / "proxy.mp4").exists()


def test_the_formats_are_the_demuxers_of_the_extensions_lumiverb_takes_in():
    assert set(ffmpeg_run.DEMUXERS) >= VIDEO_EXTENSIONS, "every video extension needs its demuxer"
    assert ffmpeg_run.INPUT_FORMATS == "matroska,mov"
    assert ffmpeg_run.input_args() == ["-protocol_whitelist", "file", "-format_whitelist", "matroska,mov"]


def test_each_format_names_one_of_this_ffmpegs_demuxers():
    out = subprocess.run(["ffmpeg", "-hide_banner", "-demuxers"], capture_output=True, text=True, check=True).stdout
    # " D   mov,mp4,m4a,3gp,3g2,mj2 QuickTime / MOV" (" D d lavfi ..." for a device)
    rows = [line.split() for line in out.splitlines()]
    names = {name for row in rows if len(row) > 2 and row[0] == "D"
             for name in (row[2] if row[1] == "d" else row[1]).split(",")}
    for demuxer in ffmpeg_run.INPUT_FORMATS.split(","):
        assert demuxer in names


@pytest.mark.parametrize("ext", VIDEO_EXTENSIONS_LIST)
def test_every_container_lumiverb_takes_in_still_opens(ext, tmp_path):
    clip = tmp_path / f"clip{ext}"
    try:
        _clip(clip)
    except subprocess.CalledProcessError:
        pytest.skip(f"this ffmpeg can't write {ext}")
    facet = probe_video(clip)
    assert facet.container == ffmpeg_run.DEMUXERS[ext]
    assert facet.duration_sec == pytest.approx(2.0, abs=0.1)
    analysis_proxy.render_analysis_proxy(clip, tmp_path / "proxy.mp4")
    assert (tmp_path / "proxy.mp4").stat().st_size > 0
    # In upper case too, as cameras name them.
    upper = clip.rename(tmp_path / f"CLIP{ext.upper()}")
    assert probe_video(upper).container == ffmpeg_run.DEMUXERS[ext]


def test_the_analysis_proxy_opens_its_original_with_the_whitelists():
    for hwaccel in (None, "videotoolbox", "vulkan"):
        cmd = analysis_proxy.build_command(Path("/in.mov"), Path("/out.mp4"), hwaccel=hwaccel)
        assert _whitelisted_before(cmd, Path("/in.mov"))
        assert cmd.index("-format_whitelist") < cmd.index("-i")
    cmd = speech_wav_command(Path("/in.mp4"), Path("/s.wav"), [AudioTrack(0, 2, "aac")])
    assert cmd.index("-protocol_whitelist") < cmd.index("-i")
