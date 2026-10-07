"""Video/audio probe facets (ADR-016 phase 1).

An editor export needs a clip's real duration, frame rate (including
drop-frame), start timecode, display dimensions and audio layout. These
come from one ffprobe pass; parse_ffprobe() turns its JSON into a facet.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.client.video.probe import VideoFacet, parse_ffprobe, probe_video

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
