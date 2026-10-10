"""Video/audio probe facets (ADR-016 phase 1).

An editor export needs a clip's real duration, frame rate (including
drop-frame), start timecode, display dimensions and audio layout. These
come from one ffprobe pass; parse_ffprobe() turns its JSON into a facet.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.processing.video.probe import VideoFacet, parse_ffprobe, probe_video

FIXTURES = Path("clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/Fixtures")


def _video(**overrides) -> dict:
    stream = {
        "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
        "r_frame_rate": "30000/1001", "avg_frame_rate": "21200/707", "duration": "7.070000",
    }
    stream.update(overrides)
    return stream


def _audio(**overrides) -> dict:
    stream = {
        "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2,
        "channel_layout": "stereo", "r_frame_rate": "0/0", "avg_frame_rate": "0/0",
    }
    stream.update(overrides)
    return stream


def _probe(*streams: dict, **format_overrides) -> dict:
    fmt = {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "7.070000"}
    fmt.update(format_overrides)
    return {"streams": list(streams), "format": fmt}


@pytest.mark.fast
def test_parse_iphone_clip() -> None:
    facet = parse_ffprobe(_probe(_audio(), _video(), {"codec_type": "data", "tags": {}}))

    assert facet == VideoFacet(
        duration_sec=7.07,
        container="mov",
        video_codec="h264",
        width=1920,
        height=1080,
        rotation=0,
        frame_rate_num=30000,
        frame_rate_den=1001,
        start_timecode=None,
        drop_frame=None,
        audio_codec="aac",
        audio_channels=2,
        audio_sample_rate=48000,
    )
    assert facet.frame_rate == pytest.approx(29.97, abs=0.001)


@pytest.mark.fast
@pytest.mark.parametrize(
    ("stream_extra", "rotation"),
    [
        ({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]}, 90),
        ({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": 90}]}, 270),
        ({"tags": {"rotate": "270"}}, 270),
        ({"side_data_list": [{"side_data_type": "Display Matrix", "rotation": 180}]}, 180),
    ],
)
def test_rotation_gives_display_dimensions(stream_extra: dict, rotation: int) -> None:
    facet = parse_ffprobe(_probe(_video(**stream_extra)))

    assert facet.rotation == rotation
    if rotation in (90, 270):
        assert (facet.width, facet.height) == (1080, 1920)
    else:
        assert (facet.width, facet.height) == (1920, 1080)


@pytest.mark.fast
def test_drop_frame_timecode_from_tmcd_stream() -> None:
    tmcd = {"codec_type": "data", "codec_tag_string": "tmcd", "tags": {"timecode": "01:00:00;00"}}

    facet = parse_ffprobe(_probe(_video(), tmcd))

    assert facet.start_timecode == "01:00:00;00"
    assert facet.drop_frame is True


@pytest.mark.fast
def test_non_drop_timecode_from_video_stream_tags() -> None:
    facet = parse_ffprobe(_probe(_video(r_frame_rate="25/1", tags={"timecode": "10:00:00:00"})))

    assert facet.start_timecode == "10:00:00:00"
    assert facet.drop_frame is False
    assert (facet.frame_rate_num, facet.frame_rate_den) == (25, 1)


@pytest.mark.fast
def test_timecode_from_format_tags() -> None:
    facet = parse_ffprobe(_probe(_video(), tags={"timecode": "00:59:58:12"}))

    assert facet.start_timecode == "00:59:58:12"


@pytest.mark.fast
def test_implausible_nominal_rate_falls_back_to_average() -> None:
    facet = parse_ffprobe(_probe(_video(r_frame_rate="90000/1", avg_frame_rate="50/2")))

    assert (facet.frame_rate_num, facet.frame_rate_den) == (25, 1)


@pytest.mark.fast
def test_video_without_audio() -> None:
    facet = parse_ffprobe(_probe(_video()))

    assert facet.audio_codec is None
    assert facet.audio_channels is None
    assert facet.audio_sample_rate is None


@pytest.mark.fast
def test_duration_falls_back_to_stream() -> None:
    data = _probe(_video(duration="12.5"))
    del data["format"]["duration"]

    assert parse_ffprobe(data).duration_sec == 12.5


@pytest.mark.fast
def test_to_dict_round_trips() -> None:
    facet = parse_ffprobe(_probe(_audio(), _video()))

    assert VideoFacet(**facet.to_dict()) == facet


@pytest.mark.fast
@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_probe_real_fixture() -> None:
    facet = probe_video(FIXTURES / "transcribe-english-brownfox-480.mov")

    assert facet.video_codec == "h264"
    assert (facet.width, facet.height) == (640, 360)
    assert (facet.frame_rate_num, facet.frame_rate_den) == (30000, 1001)
    assert facet.audio_channels == 2
    assert facet.audio_sample_rate == 48000
    assert facet.duration_sec == pytest.approx(7.07, abs=0.01)


# ---------------------------------------------------------------------------
# Scan sends the probe with ingest
# ---------------------------------------------------------------------------


def _scan_video(tmp_path: Path, probe) -> dict:
    """Run _scan_one_video with media work mocked; return the /v1/ingest form data."""
    import threading
    from unittest.mock import MagicMock, patch

    from src.processing import scan

    (tmp_path / "clip.mov").write_bytes(b"not really a movie")
    client = MagicMock()
    client.post.return_value.json.return_value = {"asset_id": "ast_1"}
    stats = MagicMock(lock=threading.Lock(), failed=0, new=0)
    with (
        patch.object(scan, "_extract_video_poster", return_value=(b"jpg", 1920, 1080)),
        patch.object(scan, "_build_exif_payload", return_value={}),
        patch.object(scan, "_generate_video_preview", return_value=None),
        patch.object(scan, "_jpeg_to_webp", return_value=b"webp"),
        patch.object(scan, "probe_video", side_effect=probe),
    ):
        scan._scan_one_video(
            client=client, library_id="lib_1", root_path=tmp_path,
            f={"rel_path": "clip.mov", "file_size": 18, "file_mtime": None},
            proxy_cache=MagicMock(), stats=stats, progress=MagicMock(), task_id=None,
            counter_field="new",
        )
    [ingest] = [c for c in client.post.call_args_list if c[0][0] == "/v1/ingest"]
    return ingest[1]["data"]


@pytest.mark.fast
def test_scan_sends_video_facet(tmp_path: Path) -> None:
    import json

    facet = parse_ffprobe(_probe(_audio(), _video()))

    data = _scan_video(tmp_path, lambda path: facet)

    assert json.loads(data["video_facet"]) == facet.to_dict()


@pytest.mark.fast
def test_scan_ingests_even_when_probe_fails(tmp_path: Path) -> None:
    import subprocess

    def fail(path):
        raise subprocess.CalledProcessError(1, "ffprobe")

    data = _scan_video(tmp_path, fail)

    assert "video_facet" not in data
    assert data["media_type"] == "video"


@pytest.mark.fast
@pytest.mark.parametrize("tag", ["01:00:00:00.5", "garbage", "25:00"])
def test_malformed_timecode_tag_is_dropped(tag: str) -> None:
    facet = parse_ffprobe(_probe(_video(tags={"timecode": tag})))

    assert facet.start_timecode is None
    assert facet.drop_frame is None


@pytest.mark.fast
def test_probe_cleans_values_it_cannot_trust() -> None:
    """Odd files report zero sizes or rates, odd rotations, or bad durations;
    those become unknown (or the nearest right angle) instead of a facet the
    server would refuse."""
    data = _probe(
        _video(width=0, height=0, side_data_list=[{"rotation": -88}]),
        _audio(sample_rate="0", channels=0),
        duration="-1",
    )

    facet = parse_ffprobe(data)

    assert (facet.width, facet.height) == (None, None)
    assert facet.rotation == 90
    assert facet.audio_sample_rate is None
    assert facet.audio_channels is None
    assert facet.duration_sec == 7.07  # the stream's own duration


@pytest.mark.fast
def test_probe_ignores_non_finite_duration() -> None:
    data = _probe(_video(duration="nan"), duration="inf")

    assert parse_ffprobe(data).duration_sec is None


@pytest.mark.fast
@pytest.mark.parametrize("rotation", ["inf", "nan", "-inf"])
def test_non_finite_rotation_means_upright(rotation: str) -> None:
    facet = parse_ffprobe(_probe(_video(tags={"rotate": rotation})))

    assert facet.rotation == 0
