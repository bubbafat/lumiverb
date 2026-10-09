"""Editor export providers (ADR-016 phase 1): a project becomes a bin of master clips.

FCP7 XML (xmeml v5) is what DaVinci Resolve and Premiere Pro import;
FCPXML 1.8 is Final Cut Pro's (Resolve imports it too). Both point at the original files and
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


def _file_of(root: ET.Element, master: ET.Element) -> ET.Element:
    """The full <file> definition a master clip refers to (by id)."""
    file_id = master.find(".//file").get("id")
    return next(f for f in root.iter("file") if f.get("id") == file_id and f.find("pathurl") is not None)


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
    root = _render("fcp7")
    clip = root.findall("bin/children/clip")[0]
    file_ = _file_of(root, clip)

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
    root = _render("fcp7")
    clip = root.findall("bin/children/clip")[1]
    file_ = _file_of(root, clip)

    assert clip.findtext("duration") == "100"
    assert clip.findtext("rate/timebase") == "25"
    assert clip.findtext("rate/ntsc") == "FALSE"
    assert file_.findtext("timecode/frame") == "900000"
    assert file_.findtext("timecode/displayformat") == "NDF"
    assert file_.find("media/audio") is None


def test_fcp7_unprobed_clip_falls_back_to_30fps() -> None:
    root = _render("fcp7", ExportBin(name="x", clips=[UNPROBED]))
    clip = root.find("bin/children/clip")

    assert clip.findtext("rate/timebase") == "30"
    assert clip.findtext("rate/ntsc") == "FALSE"
    assert clip.findtext("duration") == "60"
    assert _file_of(root, clip).findtext("timecode/frame") == "0"


def test_fcp7_files_defined_once_and_referenced_by_id() -> None:
    """xmeml defines each file once (with its pathurl); later uses are bare
    references to that id."""
    root = _render("fcp7")
    defined = [f.get("id") for f in root.iter("file") if f.find("pathurl") is not None]
    referenced = {f.get("id") for f in root.iter("file") if f.find("pathurl") is None}

    assert len(defined) == len(set(defined)) == 2
    assert referenced <= set(defined)


# ---------------------------------------------------------------------------
# FCPXML
# ---------------------------------------------------------------------------


def test_fcpxml_event_of_asset_clips() -> None:
    root = _render("fcpxml")

    assert root.tag == "fcpxml" and root.get("version") == "1.8"
    event = root.find("library/event")
    assert event.get("name") == "Customer Video <123>"
    clips = event.findall("asset-clip")
    assert event.find("project").get("name") == "Customer Video <123>"
    assert [c.get("name") for c in clips] == ["My Clip & Co.mov", "pal.mxf"]


def test_fcpxml_asset_and_format() -> None:
    root = _render("fcpxml")
    clip = root.find("library/event/asset-clip")
    asset = root.find(f"resources/asset[@id='{clip.get('ref')}']")
    fmt = root.find(f"resources/format[@id='{asset.get('format')}']")

    assert fmt.get("frameDuration") == "1001/30000s"
    assert (fmt.get("width"), fmt.get("height")) == ("640", "360")
    assert asset.get("src") == "file:///Volumes/DAS/clips/My%20Clip%20%26%20Co.mov"
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


# ---------------------------------------------------------------------------
# Hardening from the phase 1 validation review
# ---------------------------------------------------------------------------


def test_fcp7_includes_a_timeline_of_all_clips_in_order() -> None:
    """Resolve imports FCP7 XML as timelines: the bin alone may bring in
    nothing. The sequence lays every clip end to end, in bin order."""
    root = _render("fcp7")
    sequence = root.find("bin/children/sequence")

    assert sequence is not None
    assert sequence.findtext("name") == "Customer Video <123>"
    items = sequence.findall("media/video/track/clipitem")
    assert [i.findtext("name") for i in items] == ["My Clip & Co.mov", "pal.mxf"]
    # end to end on the lead's 29.97 grid: 212 frames, then PAL's 4.0 s -> 119.88,
    # rounded down so the item never runs past the end of PAL's media
    assert [(i.findtext("start"), i.findtext("end")) for i in items] == [("0", "212"), ("212", "331")]
    # each item points at its master clip's file
    master_files = {c.findtext("name"): c.find(".//file").get("id") for c in root.findall("bin/children/clip")}
    assert [i.find("file").get("id") for i in items] == [master_files["My Clip & Co.mov"], master_files["pal.mxf"]]


def test_fcp7_master_clip_carries_audio() -> None:
    clips = _render("fcp7").findall("bin/children/clip")
    with_audio, without = clips[0], clips[1]

    audio_items = with_audio.findall("media/audio/track/clipitem")
    assert len(audio_items) == 1
    assert audio_items[0].find("file").get("id") == with_audio.find("media/video/track/clipitem/file").get("id")
    assert audio_items[0].findtext("sourcetrack/mediatype") == "audio"
    assert without.find("media/audio") is None


@pytest.mark.parametrize(("num", "den"), [(10351, 346), (2997, 100), (29970, 1000)])
def test_odd_rates_snap_to_standard(num: int, den: int) -> None:
    """A variable-frame-rate phone clip can average 29.92 fps; editors want
    a standard rate (29.97 here)."""
    clip = ExportClip(**{**IPHONE.__dict__, "frame_rate_num": num, "frame_rate_den": den})

    assert clip.rate == (30000, 1001)
    root = _render("fcpxml", ExportBin(name="x", clips=[clip]))
    assert root.find("resources/format").get("frameDuration") == "1001/30000s"


def test_unusual_but_real_rate_is_kept() -> None:
    clip = ExportClip(**{**PAL.__dict__, "frame_rate_num": 15, "frame_rate_den": 1})

    assert clip.rate == (15, 1)


@pytest.mark.parametrize("tc", ["garbage", "01:00:00:00.5", "99:99"])
def test_bad_timecode_never_breaks_an_export(tc: str) -> None:
    clip = ExportClip(**{**PAL.__dict__, "start_timecode": tc})

    for provider_id in ("fcp7", "fcpxml"):
        _render(provider_id, ExportBin(name="x", clips=[clip]))
    root = _render("fcp7", ExportBin(name="x", clips=[clip]))
    file_ = _file_of(root, root.find("bin/children/clip"))
    assert file_.findtext("timecode/string") == "00:00:00:00"
    assert file_.findtext("timecode/frame") == "0"


@pytest.mark.parametrize(
    ("num", "den", "timebase", "ntsc"),
    [(24000, 1001, "24", "TRUE"), (50, 1, "50", "FALSE"), (60000, 1001, "60", "TRUE"), (24, 1, "24", "FALSE")],
)
def test_common_rates(num: int, den: int, timebase: str, ntsc: str) -> None:
    clip = ExportClip(**{**PAL.__dict__, "frame_rate_num": num, "frame_rate_den": den, "start_timecode": None})
    master = _render("fcp7", ExportBin(name="x", clips=[clip])).find("bin/children/clip")

    assert (master.findtext("rate/timebase"), master.findtext("rate/ntsc")) == (timebase, ntsc)
    fmt = _render("fcpxml", ExportBin(name="x", clips=[clip])).find("resources/format")
    assert fmt.get("frameDuration") == f"{den}/{num}s"


# ---------------------------------------------------------------------------
# An independent reader: OpenTimelineIO's FCP7 and FCPXML adapters
# ---------------------------------------------------------------------------


def _otio_clips(provider_id: str, bin_: ExportBin = BIN) -> list:
    import opentimelineio as otio

    adapter = {"fcp7": "fcp_xml", "fcpxml": "fcpx_xml"}[provider_id]
    result = otio.adapters.read_from_string(EXPORT_PROVIDERS[provider_id].render(bin_).decode(), adapter)
    timelines = [result] if isinstance(result, otio.schema.Timeline) else [
        t for t in result.find_children() if isinstance(t, otio.schema.Timeline)
    ] if hasattr(result, "find_children") else []
    if isinstance(result, otio.schema.SerializableCollection):
        timelines = [t for t in result if isinstance(t, otio.schema.Timeline)]
    assert timelines, "no timeline in the export"
    return [c for t in timelines for c in t.find_clips()]


@pytest.mark.parametrize("provider_id", ["fcp7", "fcpxml"])
def test_independent_reader_finds_every_clip_online(provider_id: str) -> None:
    """Resolve imports these formats as timelines and resolves each clip's
    file as it reads. If a reader built the same way finds a clip with no
    media, that clip would import offline."""
    from urllib.parse import unquote

    import opentimelineio as otio

    clips = _otio_clips(provider_id)

    assert clips
    for clip in clips:
        assert isinstance(clip.media_reference, otio.schema.ExternalReference), clip.name
    paths = {unquote(c.media_reference.target_url).replace("file://localhost", "file://") for c in clips}
    assert paths == {"file:///Volumes/DAS/clips/My Clip & Co.mov", "file:///Volumes/DAS/pal.mxf"}


@pytest.mark.parametrize("provider_id", ["fcp7", "fcpxml"])
def test_independent_reader_sees_clip_lengths(provider_id: str) -> None:
    clips = _otio_clips(provider_id)
    seconds = {c.name: round(c.duration().to_seconds(), 1) for c in clips}

    assert seconds["My Clip & Co.mov"] == 7.1
    assert seconds["pal.mxf"] == 4.0


def test_a_photo_is_a_five_second_still_in_the_bin_and_on_the_timeline() -> None:
    # Robert, Oct 9: exports include photos.
    from src.server.export import STILL_SEC, still
    from src.server.export.fcp7 import Fcp7XmlProvider
    from src.server.export.fcpxml import FcpxmlProvider

    photo = still("ast_p", "beach.jpg", "/Volumes/DAS/beach.jpg", 4032, 3024)
    assert STILL_SEC == 5.0 and photo.duration_frames == 150 and photo.audio_channels is None
    bin_ = ExportBin(name="Mixed", clips=[photo, PAL])

    root = ET.fromstring(Fcp7XmlProvider().render(bin_))
    seq = root.find("bin/children/sequence")
    assert seq.findtext("rate/timebase") == "25"  # the video leads the timeline, not the photo
    items = seq.findall("media/video/track/clipitem")
    assert [i.findtext("name") for i in items] == ["beach.jpg", PAL.name]
    assert items[0].findtext("duration") == "125"  # 5 s at 25 fps
    master = next(c for c in root.findall("bin/children/clip") if c.findtext("name") == "beach.jpg")
    assert master.find("media/audio") is None

    root = ET.fromstring(FcpxmlProvider().render(bin_))
    asset = next(a for a in root.iter("asset") if a.get("name") == "beach.jpg")
    assert asset.get("duration") == "0s" and asset.get("hasAudio") == "0"
    fmt = next(f for f in root.iter("format") if f.get("id") == asset.get("format"))
    assert fmt.get("name") == "FFVideoFormatRateUndefined" and fmt.get("frameDuration") is None
    on_timeline = root.find("library/event/project/sequence/spine/video")
    assert on_timeline.get("ref") == asset.get("id") and on_timeline.get("duration") == "125/25s"


def test_a_bin_of_only_photos_still_has_a_timeline() -> None:
    from src.server.export import still
    from src.server.export.fcp7 import Fcp7XmlProvider

    bin_ = ExportBin(name="Photos", clips=[still("a", "a.jpg", "/a.jpg", 100, 100)])
    root = ET.fromstring(Fcp7XmlProvider().render(bin_))
    assert root.find("bin/children/sequence/duration").text == "150"


def test_a_timeline_of_only_photos_has_a_frame_rate_in_fcpxml() -> None:
    # Review: the sequence took the photo's rate-less format, and a sequence needs one.
    from src.server.export import still
    from src.server.export.fcpxml import FcpxmlProvider

    root = ET.fromstring(FcpxmlProvider().render(ExportBin(name="Photos", clips=[
        still("a", "a.jpg", "/a.jpg", 4032, 3024), still("b", "b.jpg", "/b.jpg", 3024, 4032)])))
    sequence = root.find("library/event/project/sequence")
    fmt = root.find(f"resources/format[@id='{sequence.get('format')}']")
    assert fmt.get("frameDuration") == "1/30s" and fmt.get("name") is None
    assert (fmt.get("width"), fmt.get("height")) == ("1920", "1080")
    assert [v.get("duration") for v in sequence.findall("spine/video")] == ["150/30s", "150/30s"]
    assert sequence.get("duration") == "300/30s"


@pytest.mark.parametrize("num, den, frames", [(30000, 1001, 150), (24000, 1001, 120), (25, 1, 125)])
def test_a_still_is_five_seconds_to_the_nearest_frame(num: int, den: int, frames: int) -> None:
    # A still has no end of media to stay inside: 5 s on a 29.97 timeline is
    # 150 frames, not 149 (4.97 s).
    from src.server.export import still

    lead = ExportClip(**{**PAL.__dict__, "frame_rate_num": num, "frame_rate_den": den, "start_timecode": None})
    bin_ = ExportBin(name="x", clips=[lead, still("p", "p.jpg", "/p.jpg", 100, 100)])
    item = _render("fcp7", bin_).findall("bin/children/sequence/media/video/track/clipitem")[1]
    assert int(item.findtext("end")) - int(item.findtext("start")) == frames
    video = _render("fcpxml", bin_).find("library/event/project/sequence/spine/video")
    assert video.get("duration") == f"{frames * den}/{num}s"


def test_timeline_never_runs_past_a_clips_media() -> None:
    """A 30 fps clip of 7.2 s on a 23.976 timeline: 172 frames (7.17 s),
    never 173 (7.22 s, past the end of the file)."""
    lead = ExportClip(**{**PAL.__dict__, "frame_rate_num": 24000, "frame_rate_den": 1001,
                         "start_timecode": None})
    other = ExportClip(**{**PAL.__dict__, "asset_id": "ast_9", "name": "thirty.mov",
                          "path": "/Volumes/DAS/thirty.mov", "duration_sec": 7.2,
                          "frame_rate_num": 30, "frame_rate_den": 1, "start_timecode": None})
    bin_ = ExportBin(name="x", clips=[lead, other])

    item = _render("fcp7", bin_).findall("bin/children/sequence/media/video/track/clipitem")[1]
    assert int(item.findtext("end")) - int(item.findtext("start")) == 172
    spine = _render("fcpxml", bin_).findall("library/event/project/sequence/spine/asset-clip")
    assert spine[1].get("duration") == f"{172 * 1001}/24000s"


def test_timeline_takes_its_frame_size_from_a_probed_clip() -> None:
    root = _render("fcpxml", ExportBin(name="x", clips=[UNPROBED, IPHONE]))
    sequence = root.find("library/event/project/sequence")
    fmt = root.find(f"resources/format[@id='{sequence.get('format')}']")

    assert (fmt.get("width"), fmt.get("height")) == ("640", "360")


def test_export_filename_drops_control_characters() -> None:
    from src.server.api.routers.projects import _export_filename

    assert _export_filename("a\x7fb\x01c", ".xml") == "a_b_c.xml"
