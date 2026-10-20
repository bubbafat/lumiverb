"""What a server tells a reader to make things with (reader-changes'
made_with: a device's, a scan's) is held to what this version declares
(ADR-018 decision 6: typed values from known ranges, never a string where a
number goes): src/shared/producers.py checked. Whatever is told, nothing but
digits reaches ffmpeg's arguments or a filter's text, as every command a
setting goes into is built through ffmpeg_run.whole. A value pasted into a
filter (the analysis proxy's max_edge) could otherwise add filters of its
own, which read and write files.

Hostile values for every setting a reader is told: each refused, and the
command never built."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.processing.producer_settings import ProducerSettings
from src.processing.scan import Previews
from src.processing.video import analysis_proxy, ffmpeg_run
from src.producers.names import ANALYSIS_PROXY, PREVIEW, VIDEO_PREVIEW
from src.server.check_ins import READER_MAKES
from src.shared.producers import PRODUCERS, SettingsRefusedError, checked, effective_settings

pytestmark = pytest.mark.fast

# What a tampered answer might carry where a number goes.
HOSTILE = ["960", "960':h=1,movie=/etc/passwd[x];[x]overlay", "28; touch /tmp/x", "1,2", "", "\x00", -1, 10**9,
           float("nan"), float("inf"), True, 28.5, None, [960], {"v": 960}]


def _told(artifact: str) -> dict:
    return {"producer": PRODUCERS[artifact].artifact, "version": PRODUCERS[artifact].version,
            "settings": effective_settings(artifact)}


def test_every_producers_own_settings_are_taken_as_they_are():
    for artifact in PRODUCERS:
        assert checked(artifact, effective_settings(artifact)) == effective_settings(artifact), artifact


@pytest.mark.parametrize("artifact", READER_MAKES)
def test_a_hostile_value_for_any_setting_a_reader_is_told_is_refused(artifact):
    told = effective_settings(artifact)
    for key in told:
        for value in HOSTILE:
            with pytest.raises(SettingsRefusedError):
                checked(artifact, {**told, key: value})


def test_a_quality_is_only_what_one_of_its_choices_stands_for():
    with pytest.raises(SettingsRefusedError, match="crf isn't what any Quality stands for"):
        checked(ANALYSIS_PROXY, {**effective_settings(ANALYSIS_PROXY), "crf": 27})  # in x264's range, not a choice
    with pytest.raises(SettingsRefusedError):  # each number a choice's, but not the same choice's
        checked(PREVIEW, {**effective_settings(PREVIEW), "jpeg_quality": 75, "webp_quality": 90})
    assert checked(PREVIEW, {**effective_settings(PREVIEW), "jpeg_quality": 90, "webp_quality": 90})["webp_quality"] == 90


def test_settings_missing_one_or_with_one_it_doesnt_know_are_refused():
    told = effective_settings(ANALYSIS_PROXY)
    for odd in ({k: v for k, v in told.items() if k != "fps_max"}, {**told, "vf": "scale=1"}, [told], "max_edge=960"):
        with pytest.raises(SettingsRefusedError):
            checked(ANALYSIS_PROXY, odd)


def test_a_scan_makes_nothing_with_preview_settings_it_refuses():
    good = Previews.told({PREVIEW: _told(PREVIEW), VIDEO_PREVIEW: _told(VIDEO_PREVIEW)})
    assert good.video_preview == effective_settings(VIDEO_PREVIEW)
    bad = _told(VIDEO_PREVIEW)
    bad["settings"] = {**bad["settings"], "crf": "28 -vf movie=/etc/passwd"}
    with pytest.raises(SettingsRefusedError):
        Previews.told({PREVIEW: _told(PREVIEW), VIDEO_PREVIEW: bad})
    older = {**_told(VIDEO_PREVIEW), "version": "0", "settings": {"crf": "anything"}}
    assert Previews.told({VIDEO_PREVIEW: older}).video_preview == effective_settings(VIDEO_PREVIEW), \
        "another version's: this one's own, as before"


def test_a_device_makes_nothing_of_a_producer_whose_settings_it_refuses_and_says_so(caplog):
    settings = ProducerSettings(None, fetch=False)
    bad = _told(ANALYSIS_PROXY)
    bad["settings"] = {**bad["settings"], "max_edge": "960':h=1,movie=/etc/passwd"}
    with caplog.at_level(logging.ERROR):
        settings.told({ANALYSIS_PROXY: bad, VIDEO_PREVIEW: _told(VIDEO_PREVIEW)})
        settings.told({ANALYSIS_PROXY: bad})
    assert settings.refused[ANALYSIS_PROXY] == "Analysis proxies: Longest edge is a number", "said as Settings says it"
    with pytest.raises(SettingsRefusedError):
        settings.settings(ANALYSIS_PROXY)
    assert settings.settings(VIDEO_PREVIEW) == effective_settings(VIDEO_PREVIEW), "the others as told"
    assert len([r for r in caplog.records if "refused" in r.getMessage()]) == 1, "said once, not every check-in"

    settings.told({ANALYSIS_PROXY: _told(ANALYSIS_PROXY)})
    assert ANALYSIS_PROXY not in settings.refused and settings.settings(ANALYSIS_PROXY)["max_edge"] == 960


def test_a_render_is_built_from_numbers_alone():
    for field, value in (("max_edge", "960':h=1,movie=/etc/passwd"), ("fps_max", 30.5), ("crf", True),
                         ("audio_kbps_per_channel", "48"), ("gpu_decodes", -1)):
        with pytest.raises(ValueError, match=field):
            analysis_proxy.AnalysisProxySettings(**{field: value})
    for field, value in (("encoder", "libx264 -vf movie=x"), ("decoder", "cuda -i /etc/passwd")):
        with pytest.raises(ValueError, match=field):
            analysis_proxy.AnalysisProxySettings(**{field: value})
    built = analysis_proxy.build_command(Path("in.mov"), Path("out.mp4"),
                                         analysis_proxy.AnalysisProxySettings(max_edge=640))
    assert "scale=w='min(iw,640)':h='min(ih,640)'" in built[built.index("-vf") + 1]


def test_a_video_preview_is_built_from_a_number_alone(monkeypatch):
    from src.processing import ingest

    ran: list[list[str]] = []
    monkeypatch.setattr(ffmpeg_run, "run", lambda cmd, **_: ran.append(cmd))
    for crf in ("28 -vf movie=/etc/passwd", 28.0, True, 99):
        with pytest.raises(ValueError, match="crf"):
            ingest._generate_video_preview(Path("in.mov"), crf=crf)
    assert ran == [], "no ffmpeg run"


def test_a_decoder_is_a_name_alone():
    for choice in ("cuda -i /etc/passwd", "vulkan,movie", "-xerror", "../x"):
        with pytest.raises(ValueError, match="decoder"):
            ffmpeg_run.decoded_by(choice)
    assert ffmpeg_run.decoded_by("cuda") == "cuda" and ffmpeg_run.decoded_by("cpu") is None
