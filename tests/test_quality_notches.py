# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Quality as a notch, not an encoder's number (ADR-018 decision 6, phase 7).

The analysis proxy's quality is good, better or best, and so are a clip's
preview and thumbnail and a video's preview, which a scan makes: it reads
the account's from what the server tells the library's reader, and the
server keeps previews and thumbnails at it. Lineage hashes what a
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


# ---------------------------------------------------------------------------
# Previews: the scan makes them at the account's notch; the server keeps them so
# ---------------------------------------------------------------------------

BEFORE_PREVIEW_NOTCHES = {
    "proxy": {"long_edge": 2048, "jpeg_quality": 75, "webp_quality": 80, "thumbnail_edge": 512},
    "video_preview": {"seconds": 10, "max_height": 720, "crf": 28, "audio_kbps": 128},
}


@pytest.mark.fast
@pytest.mark.parametrize("artifact", ["proxy", "video_preview"])
def test_previews_at_better_are_what_scans_made_before_notches(artifact):
    assert shared.effective_settings(artifact) == BEFORE_PREVIEW_NOTCHES[artifact]
    assert shared.settings_hash(shared.effective_settings(artifact)) == shared.settings_hash(
        BEFORE_PREVIEW_NOTCHES[artifact])


def _made_with(notch: str, **changed) -> dict:
    """reader-changes' made_with, as the server says it for an account at this notch."""
    out = {}
    for artifact in ("proxy", "video_preview"):
        spec = shared.PRODUCERS[artifact]
        out[artifact] = {"producer": spec.producer, "version": spec.version,
                         "settings": shared.effective_settings(artifact, {"quality": notch})}
    for artifact, said in changed.items():
        out[artifact] = {**out[artifact], **said}
    return out


@pytest.mark.fast
def test_the_scan_takes_the_accounts_preview_settings_unless_they_name_another_version():
    from src.processing.scan import Previews

    best = Previews.told(_made_with("best"))
    assert (best.proxy["jpeg_quality"], best.proxy["webp_quality"], best.video_preview["crf"]) == (90, 90, 23)
    assert best.lineage("proxy", "sha256:" + "ab" * 32)["settings_hash"] == shared.settings_hash(
        shared.effective_settings("proxy", {"quality": "best"}))
    assert Previews.told({}) == Previews()  # an older server: the registry's
    other = Previews.told(_made_with("best", proxy={"version": "99"}))  # this client can't make that one
    assert other.proxy == shared.effective_settings("proxy") and other.video_preview["crf"] == 23


@pytest.mark.fast
def test_the_scan_makes_a_photos_and_a_videos_previews_at_the_accounts_notch(tmp_path):
    import json
    from unittest.mock import MagicMock, patch

    from src.processing import scan
    from tests.test_producer_settings_client import SHA, _scan_args

    previews = scan.Previews.told(_made_with("good"))
    args = _scan_args(tmp_path, "image")
    with (
        patch.object(scan, "_generate_proxy_bytes", return_value=(b"jpeg", 10, 10)) as proxy,
        patch.object(scan, "_build_exif_payload", return_value={}),
        patch.object(scan, "_jpeg_to_webp", return_value=b"webp") as webp,
    ):
        scan._scan_one(**args, previews=previews)
    assert proxy.call_args.kwargs["jpeg_quality"] == 65 and webp.call_args.args[1] == 70
    [call] = [c for c in args["client"].post.call_args_list if c.args[0] == "/v1/ingest"]
    assert json.loads(call.kwargs["data"]["lineage"])["proxy"] == shared.lineage(
        "proxy", shared.effective_settings("proxy", {"quality": "good"}), SHA)

    args = _scan_args(tmp_path, "video")
    facet = MagicMock()
    facet.to_dict.return_value = {"frame_rate": 25.0}
    with (
        patch.object(scan, "_extract_video_poster", return_value=(b"jpeg", 10, 10)) as poster,
        patch.object(scan, "_build_exif_payload", return_value={}),
        patch.object(scan, "_generate_video_preview", return_value=b"mp4") as preview,
        patch.object(scan, "_jpeg_to_webp", return_value=b"webp"),
        patch.object(scan, "probe_video", return_value=facet),
    ):
        scan._scan_one_video(**args, previews=previews)
    assert poster.call_args.kwargs["jpeg_quality"] == 65 and preview.call_args.kwargs["crf"] == 32
    posts = {c.args[0]: c for c in args["client"].post.call_args_list}
    assert json.loads(posts["/v1/assets/ast_a/artifacts/video_preview"].kwargs["data"]["lineage"]) == shared.lineage(
        "video_preview", shared.effective_settings("video_preview", {"quality": "good"}), SHA)


@pytest.mark.fast
def test_a_video_preview_is_rendered_at_its_crf(tmp_path):
    from src.processing.ingest import _generate_video_preview
    from tests.test_scan_outcomes import Lib, _video

    lib = Lib(tmp_path, {"library_id": "lib_1"})
    source = _video(lib, "a.mp4")
    good, best = _generate_video_preview(source, crf=40), _generate_video_preview(source, crf=18)
    assert good and best and len(good) < len(best)


def _noisy_jpeg() -> bytes:
    """A photo with detail, so its quality shows in its size."""
    import io
    import random

    from PIL import Image

    rnd = random.Random(7)
    img = Image.new("RGB", (640, 480))
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(640 * 480)])
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


@pytest.mark.slow
def test_the_server_keeps_previews_and_thumbnails_at_the_accounts_notch(env):
    import io
    import json

    from src.server.preview_image import reencoded, thumbnail
    from tests.test_archive_by_hand import _sha
    from tests.test_reconciler import _library

    lib = _library(env, "PreviewQuality")
    client, headers, library_id, *_ = lib
    photo = _noisy_jpeg()

    def ingest(name: str) -> str:
        sha = _sha()
        want = _proxies_of(lib, sha)
        r = client.post("/v1/ingest", headers=headers, files={"proxy": ("p.jpg", io.BytesIO(photo), "image/jpeg")},
                        data={"library_id": library_id, "rel_path": name, "file_size": "1000", "media_type": "image",
                              "width": "640", "height": "480", "exif": json.dumps({"content_hash": sha}),
                              "lineage": json.dumps({"proxy": want})})
        assert r.status_code == 200, r.text
        return r.json()["asset_id"]

    def kept(asset_id: str, artifact: str) -> bytes:
        r = client.get(f"/v1/assets/{asset_id}/artifacts/{artifact}", headers=headers)
        assert r.status_code == 200, r.text
        return r.content

    better = ingest("better.jpg")
    assert kept(better, "proxy") == reencoded(photo, fmt="WEBP", quality=80, max_long_edge=2048)[0]
    try:
        r = client.put("/v1/producers/proxy/settings", json={"settings": {"quality": "best"}, "redo": False},
                       headers=headers)
        assert r.status_code == 200, r.text  # new files only: the one made stays as it is
        best = ingest("best.jpg")
        stored = kept(best, "proxy")
        assert stored == reencoded(photo, fmt="WEBP", quality=90, max_long_edge=2048)[0]
        assert kept(best, "thumbnail") == thumbnail(stored, 90)
        assert len(stored) > len(kept(better, "proxy"))
        # One a client sends as an artifact is kept so too.
        r = client.post(f"/v1/assets/{better}/artifacts/proxy", headers=headers,
                        files={"file": ("p.jpg", io.BytesIO(photo), "image/jpeg")},
                        data={"lineage": json.dumps(_proxies_of(lib, None))})
        assert r.status_code == 200, r.text
        assert kept(better, "proxy") == reencoded(photo, fmt="WEBP", quality=90)[0]
    finally:
        client.put("/v1/producers/proxy/settings", json={"settings": {"quality": None}, "redo": True}, headers=headers)
        client.post("/v1/producers/proxy/resume", json={"scope": "redo"}, headers=headers)


def _proxies_of(lib, sha: str | None) -> dict:
    """The proxy lineage a scan would send now (the account's settings)."""
    from tests.test_lineage_api import _want

    return _want(lib, "proxy", sha)
