# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Quality as a notch, not an encoder's number (ADR-018 decision 6, phase 7).

The analysis proxy's quality is good, better or best. Lineage hashes what a
notch stands for (x264's CRF), so better, CRF 28, is what every one was
made with before notches: none is made another way for them. Each encoder
maps the number to its own (NVENC's CQ two more). Settings shows the notch;
what a producer's work, a device too, is told is the number.
"""

from __future__ import annotations

import pytest

from src.shared import producers as shared

# The analysis proxy's settings before notches, as every one made then was hashed.
BEFORE_NOTCHES = {"max_edge": 960, "fps_max": 30, "crf": 28, "audio_kbps_per_channel": 48}


@pytest.mark.fast
def test_better_is_what_was_made_before_notches_so_nothing_is_made_again():
    assert shared.effective_settings("analysis_proxy") == BEFORE_NOTCHES
    assert shared.settings_hash(shared.effective_settings("analysis_proxy")) == shared.settings_hash(BEFORE_NOTCHES)
    assert shared.notches("analysis_proxy") == {"quality": "better"}


@pytest.mark.fast
@pytest.mark.parametrize("notch, crf, cq", [("good", 32, 34), ("better", 28, 30), ("best", 23, 25)])
def test_each_notch_stands_for_a_number_each_encoder_maps_to_its_own(notch, crf, cq):
    from src.processing.video.analysis_proxy import AnalysisProxySettings, _video_codec_args

    settings = shared.effective_settings("analysis_proxy", {"quality": notch})
    assert settings["crf"] == crf and "quality" not in settings
    x264 = _video_codec_args(AnalysisProxySettings.for_producer(settings, "libx264"))
    assert x264[x264.index("-crf") + 1] == str(crf)
    nvenc = _video_codec_args(AnalysisProxySettings.for_producer(settings, "h264_nvenc"))
    assert nvenc[nvenc.index("-cq") + 1] == str(cq)
    # What the work says it made with is what the server wants now.
    assert AnalysisProxySettings.for_producer(settings, "h264_nvenc").output() == settings


@pytest.mark.fast
def test_a_notch_is_one_of_its_choices_and_one_it_doesnt_know_is_its_default():
    setting = shared.PRODUCERS["analysis_proxy"].setting("quality")
    assert setting.check("best") == "best"
    with pytest.raises(ValueError, match="one of good, better, best"):
        setting.check(23)
    assert shared.effective_settings("analysis_proxy", {"quality": "superb"})["crf"] == 28
    assert shared.notches("analysis_proxy", {"quality": "superb"}) == {"quality": "better"}
    # A CRF an account set before notches is no setting now (the migration made it a notch).
    assert shared.effective_settings("analysis_proxy", {"crf": 24})["crf"] == 28


@pytest.mark.fast
@pytest.mark.parametrize("bad", [
    {"remakes": False},  # out of the hash: what it made would never be made another way
    {"default": "superb"},
    {"means": {"good": {"crf": 32}, "better": {"crf": 28}}},  # a choice meaning nothing
    {"means": {"good": {"crf": 32}, "better": {"cq": 30}, "best": {"crf": 23}}},  # not the same keys
    {"means": {"good": {"max_edge": 640}, "better": {"max_edge": 960}, "best": {"max_edge": 1280}}},  # another's
])
def test_a_notch_declared_wrong_is_refused(bad):
    from dataclasses import replace

    from src.producers import _check
    from src.producers.contract import Setting

    p = shared.PRODUCERS["analysis_proxy"]
    notch = {"key": "quality", "default": "better", "label": "Quality", "kind": "notch",
             "choices": ("good", "better", "best"), "means": p.setting("quality").means, **bad}
    settings = tuple(Setting(**notch) if s.key == "quality" else s for s in p.settings)
    with pytest.raises(ValueError, match="notch"):
        _check([*(q for q in shared.PRODUCERS.values() if q.artifact != p.artifact), replace(p, settings=settings)])


# ---------------------------------------------------------------------------
# The API: the notch as chosen, the number as made
# ---------------------------------------------------------------------------

from tests.test_analysis_proxy_api import env  # noqa: E402, F401 — the shared server fixture


def _proxies(env) -> dict:
    client, headers, *_ = env
    r = client.get("/v1/producers", params={"counts": "false"}, headers=headers)
    assert r.status_code == 200, r.text
    return {p["artifact"]: p for p in r.json()["producers"]}["analysis_proxy"]


@pytest.mark.slow
def test_settings_show_the_notch_and_the_workers_are_told_its_number(env):
    client, headers, *_ = env
    quality = {f["key"]: f for f in _proxies(env)["fields"]}["quality"]
    assert (quality["kind"], quality["value"], quality["default"], quality["choices"]) == (
        "notch", "better", "better", ["good", "better", "best"])
    try:
        r = client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"quality": "best"}, "redo": False},
                       headers=headers)
        assert r.status_code == 200, r.text
        now = _proxies(env)
        assert {f["key"]: f for f in now["fields"]}["quality"]["value"] == "best"
        assert now["settings"]["crf"] == 23 and "quality" not in now["settings"]  # what a worker reads
        assert now["settings_hash"] == shared.settings_hash({**BEFORE_NOTCHES, "crf": 23})
        r = client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"crf": 23}}, headers=headers)
        assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_setting", r.text
        r = client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"quality": 23}}, headers=headers)
        assert r.status_code == 422 and r.json()["error"]["code"] == "bad_setting", r.text
    finally:
        client.put("/v1/producers/analysis_proxy/settings", json={"settings": {"quality": None}, "redo": True},
                   headers=headers)
        client.post("/v1/producers/analysis_proxy/resume", json={"scope": "redo"}, headers=headers)
    assert _proxies(env)["settings_hash"] == shared.settings_hash(BEFORE_NOTCHES)
