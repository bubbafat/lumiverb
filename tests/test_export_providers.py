"""Editor export providers (ADR-016 phase 1): a project becomes a bin of master clips.

FCP7 XML (xmeml v5) is what DaVinci Resolve and Premiere Pro import;
FCPXML 1.10 is Final Cut Pro's. Both point at the original files and
carry frame rate, duration in frames, start timecode and audio layout.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from src.server.export import EXPORT_PROVIDERS, ExportBin, ExportClip, timecode_to_frames

pytestmark = pytest.mark.fast

IPHONE = ExportClip(
    asset_id="ast_1",
    name="My Clip & Co.mov",
    path="/Volumes/DAS/clips/My Clip & Co.mov",
    duration_sec=7.07,
    frame_rate_num=30000,
    frame_rate_den=1001,
    width=640,
    height=360,
    start_timecode="01:00:00;00",
    drop_frame=True,
    audio_channels=2,
    audio_sample_rate=48000,
)
PAL = ExportClip(
    asset_id="ast_2",
    name="pal.mxf",
    path="/Volumes/DAS/pal.mxf",
    duration_sec=4.0,
    frame_rate_num=25,
    frame_rate_den=1,
    width=1920,
    height=1080,
    start_timecode="10:00:00:00",
    drop_frame=False,
    audio_channels=None,
    audio_sample_rate=None,
)
UNPROBED = ExportClip(
    asset_id="ast_3", name="old.mov", path="/Volumes/DAS/old.mov", duration_sec=2.0,
    frame_rate_num=None, frame_rate_den=None, width=None, height=None,
    start_timecode=None, drop_frame=None, audio_channels=None, audio_sample_rate=None,
)
BIN = ExportBin(name="Customer Video <123>", clips=[IPHONE, PAL])


def _render(provider_id: str, bin_: ExportBin = BIN) -> ET.Element:
    return ET.fromstring(EXPORT_PROVIDERS[provider_id].render(bin_))


def test_registry() -> None:
    assert EXPORT_PROVIDERS["fcp7"].label == "DaVinci Resolve / Premiere Pro"
    assert EXPORT_PROVIDERS["fcp7"].file_extension == ".xml"
    assert EXPORT_PROVIDERS["fcpxml"].label == "Final Cut Pro"
    assert EXPORT_PROVIDERS["fcpxml"].file_extension == ".fcpxml"


@pytest.mark.parametrize(
    ("tc", "timebase", "drop", "frames"),
    [
        ("10:00:00:00", 25, False, 900_000),
        ("00:00:01:05", 25, False, 30),
        ("01:00:00;00", 30, True, 107_892),
        ("00:01:00;02", 30, True, 1_800),
        ("00:10:00;00", 30, True, 17_982),
    ],
)
def test_timecode_to_frames(tc: str, timebase: int, drop: bool, frames: int) -> None:
    assert timecode_to_frames(tc, timebase, drop) == frames


# ---------------------------------------------------------------------------
# FCP7 XML
# ---------------------------------------------------------------------------


def test_fcp7_bin_of_master_clips() -> None:
    root = _render("fcp7")

    assert root.tag == "xmeml" and root.get("version") == "5"
    bin_ = root.find("bin")
    assert bin_.findtext("name") == "Customer Video <123>"
    clips = bin_.findall("children/clip")
    assert [c.findtext("name") for c in clips] == ["My Clip & Co.mov", "pal.mxf"]
    assert all(c.findtext("ismasterclip") == "TRUE" for c in clips)


def test_fcp7_ntsc_drop_frame_clip() -> None:
    clip = _render("fcp7").findall("bin/children/clip")[0]
    file_ = clip.find(".//file")

    assert clip.findtext("duration") == "212"  # round(7.07 * 29.97)
    assert clip.findtext("rate/timebase") == "30"
    assert clip.findtext("rate/ntsc") == "TRUE"
    assert file_.findtext("pathurl") == "file://localhost/Volumes/DAS/clips/My%20Clip%20%26%20Co.mov"
    assert file_.findtext("timecode/string") == "01:00:00;00"
    assert file_.findtext("timecode/frame") == "107892"
    assert file_.findtext("timecode/displayformat") == "DF"
    assert file_.findtext("media/video/samplecharacteristics/width") == "640"
    assert file_.findtext("media/audio/channelcount") == "2"
    assert file_.findtext("media/audio/samplecharacteristics/samplerate") == "48000"


def test_fcp7_pal_clip_without_audio() -> None:
    clip = _render("fcp7").findall("bin/children/clip")[1]
    file_ = clip.find(".//file")

    assert clip.findtext("duration") == "100"
    assert clip.findtext("rate/timebase") == "25"
    assert clip.findtext("rate/ntsc") == "FALSE"
    assert file_.findtext("timecode/frame") == "900000"
    assert file_.findtext("timecode/displayformat") == "NDF"
    assert file_.find("media/audio") is None


def test_fcp7_unprobed_clip_falls_back_to_30fps() -> None:
    clip = _render("fcp7", ExportBin(name="x", clips=[UNPROBED])).find("bin/children/clip")

    assert clip.findtext("rate/timebase") == "30"
    assert clip.findtext("rate/ntsc") == "FALSE"
    assert clip.findtext("duration") == "60"
    assert clip.find(".//file/timecode/frame").text == "0"


def test_fcp7_file_ids_are_unique() -> None:
    root = _render("fcp7")
    ids = [f.get("id") for f in root.iter("file")]

    assert len(ids) == len(set(ids)) == 2


# ---------------------------------------------------------------------------
# FCPXML
# ---------------------------------------------------------------------------


def test_fcpxml_event_of_asset_clips() -> None:
    root = _render("fcpxml")

    assert root.tag == "fcpxml" and root.get("version") == "1.10"
    event = root.find("library/event")
    assert event.get("name") == "Customer Video <123>"
    clips = event.findall("asset-clip")
    assert [c.get("name") for c in clips] == ["My Clip & Co.mov", "pal.mxf"]


def test_fcpxml_asset_and_format() -> None:
    root = _render("fcpxml")
    clip = root.find("library/event/asset-clip")
    asset = root.find(f"resources/asset[@id='{clip.get('ref')}']")
    fmt = root.find(f"resources/format[@id='{asset.get('format')}']")

    assert fmt.get("frameDuration") == "1001/30000s"
    assert (fmt.get("width"), fmt.get("height")) == ("640", "360")
    assert asset.find("media-rep").get("src") == "file:///Volumes/DAS/clips/My%20Clip%20%26%20Co.mov"
    assert asset.find("media-rep").get("kind") == "original-media"
    assert asset.get("duration") == "212212/30000s"  # 212 frames
    assert asset.get("start") == "107999892/30000s"  # 01:00:00;00 = 107892 frames
    assert asset.get("hasAudio") == "1"
    assert asset.get("audioChannels") == "2"
    assert asset.get("audioRate") == "48000"
    assert clip.get("duration") == asset.get("duration")
    assert clip.get("start") == asset.get("start")
    assert clip.get("tcFormat") == "DF"


def test_fcpxml_pal_without_audio() -> None:
    root = _render("fcpxml")
    clip = root.findall("library/event/asset-clip")[1]
    asset = root.find(f"resources/asset[@id='{clip.get('ref')}']")
    fmt = root.find(f"resources/format[@id='{asset.get('format')}']")

    assert fmt.get("frameDuration") == "1/25s"
    assert asset.get("duration") == "100/25s"
    assert asset.get("start") == "900000/25s"
    assert asset.get("hasAudio") == "0"
    assert clip.get("tcFormat") == "NDF"


def test_fcpxml_clips_with_same_format_share_it() -> None:
    twin = ExportClip(**{**IPHONE.__dict__, "asset_id": "ast_9", "name": "twin.mov",
                         "path": "/Volumes/DAS/twin.mov"})
    root = _render("fcpxml", ExportBin(name="x", clips=[IPHONE, twin]))

    assert len(root.findall("resources/format")) == 1
    assert len(root.findall("resources/asset")) == 2


@pytest.mark.parametrize("provider_id", ["fcp7", "fcpxml"])
def test_empty_bin_is_valid(provider_id: str) -> None:
    root = _render(provider_id, ExportBin(name="Empty", clips=[]))

    assert root is not None
