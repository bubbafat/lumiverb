"""The worker makes artifacts with the server's producer settings and says so
in lineage (ADR-016 phase 3). Where it still uses built-in constants (the
scan's proxies and previews, scene detection, face gates), those must be the
registry's settings, or its lineage would claim what it didn't do."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.shared import producers as P

pytestmark = pytest.mark.fast


def test_the_workers_constants_are_the_registrys_settings():
    import inspect

    from src.client.cli import config as cli_config
    from src.processing.proxy import proxy_gen
    from src.processing.video import scene_segmenter, video_scanner
    from src.processing.video.analysis_proxy import AnalysisProxySettings
    from src.processing.workers.embeddings import clip_provider
    from src.processing.workers.faces import insightface_provider as face

    proxy = P.effective_settings("lumiverb.preview")  # its quality notch at its default, better: what it stands for
    assert (proxy_gen.PROXY_LONG_EDGE, proxy_gen.PROXY_JPEG_QUALITY) == (proxy["long_edge"], proxy["jpeg_quality"])
    assert AnalysisProxySettings().output() == P.effective_settings("lumiverb.video.analysis_proxy")  # better: CRF 28
    # Scenes have one setting, Detail, out of the hash: what they're found with is fixed in code.
    from src.producers.scenes import detail as scene_detail

    assert P.PRODUCERS["lumiverb.video.scenes"].defaults == {}
    assert video_scanner.OUT_WIDTH == scene_detail.FRAME_WIDTH
    assert scene_segmenter.HASH_SIZE == scene_detail.HASH_SIZE == 8
    assert "select='eq(pict_type\\\\,I)'" in inspect.getsource(video_scanner)  # keyframes only
    faces = P.PRODUCERS["lumiverb.image.faces"].defaults
    assert face.MODEL_VERSION == faces["model"]
    # The rest of what faces are found with is the level's (Normal's are the provider's defaults).
    from src.producers.faces.levels import GATES

    assert face.FaceSettings().__dict__ == {"det_size": faces["det_size"], "max_detect_edge": face.MAX_DETECT_EDGE,
                                           **GATES["normal"]}
    clip = P.PRODUCERS["lumiverb.image.likeness"].defaults
    assert clip_provider.MODEL_VERSION == f"{clip['model']}-{clip['pretrained']}"
    from src.processing.proxy import proxy_cache
    from src.producers.clip import PROXY_CACHE_EDGE

    assert PROXY_CACHE_EDGE == proxy_cache._DEFAULT_MAX_EDGE == clip["input_edge"]
    # What changes the output lives only on the server: the CLI's config has none of it.
    assert not {"whisper_model", "proxy_max_edge", "analysis_proxy_max_edge", "vision_api_url",
                "vision_api_key", "vision_model_id"} & set(cli_config.CLIConfig.model_fields)


def test_the_previews_settings_are_what_the_scan_renders():
    import inspect

    from src.processing import ingest

    source = inspect.getsource(ingest._generate_video_preview)
    preview = P.effective_settings("lumiverb.video.preview")
    assert f"PREVIEW_DURATION_SEC = {preview['seconds']}" in source
    assert f"PREVIEW_MAX_HEIGHT = {preview['max_height']}" in source
    # Its quality is the account's notch (scan.Previews); its default, better's CRF.
    assert '"-crf", str(crf)' in source
    assert inspect.signature(ingest._generate_video_preview).parameters["crf"].default == preview["crf"]
    assert f'"-b:a", "{preview["audio_kbps"]}k"' in source
    proxy = P.effective_settings("lumiverb.preview")
    assert ingest.PROXY_WEBP_QUALITY == proxy["webp_quality"]
    from src.server import preview_image
    from src.server.api.routers import ingest as server_ingest
    assert server_ingest.THUMBNAIL_LONG_EDGE == proxy["thumbnail_edge"]
    assert proxy["webp_quality"] == preview_image.WEBP_QUALITY


def _client(settings: dict | None = None, fails: bool = False) -> MagicMock:
    client = MagicMock()
    if fails:
        client.get.side_effect = RuntimeError("404")
    else:
        producers = [{"artifact": a, "settings": P.effective_settings(a)} for a in P.ARTIFACTS]
        for p in producers:
            p["settings"].update((settings or {}).get(p["artifact"], {}))
        client.get.return_value.json.return_value = {"producers": producers}
    return client


def test_it_uses_the_servers_settings_and_says_so():
    from src.processing.producer_settings import ProducerSettings

    ps = ProducerSettings(_client({"lumiverb.video.transcript": {"model": "medium"}}))
    assert ps.settings("lumiverb.video.transcript")["model"] == "medium"
    lin = ps.lineage("lumiverb.video.transcript", "sha256:" + "ab" * 32)
    assert lin == {"producer": "lumiverb.video.transcript", "version": "1",
                   "settings_hash": P.settings_hash({**P.effective_settings("lumiverb.video.transcript"), "model": "medium"}),
                   "source_hash": "sha256:" + "ab" * 32}


def test_an_older_server_means_the_registrys_settings():
    from src.processing.producer_settings import ProducerSettings

    ps = ProducerSettings(_client(fails=True))
    assert ps.settings("lumiverb.video.transcript") == P.effective_settings("lumiverb.video.transcript")


def test_settings_actually_used_are_what_lineage_hashes():
    from src.processing.producer_settings import ProducerSettings

    ps = ProducerSettings(_client())
    used = {**ps.settings("lumiverb.image.likeness"), "input_edge": 2048}
    assert ps.lineage("lumiverb.image.likeness", None, used=used)["settings_hash"] == P.settings_hash(used)
    assert ps.lineage("lumiverb.image.likeness", None, used=used)["settings_hash"] != ps.lineage("lumiverb.image.likeness", None)["settings_hash"]


# ---------------------------------------------------------------------------
# The scan says how it made each clip's proxy, probe and preview.
# ---------------------------------------------------------------------------

SHA = "sha256:" + "cd" * 32


def _scan_args(tmp_path, media_type: str) -> dict:
    from src.processing.scan import ScanStats

    (tmp_path / "a.file").write_bytes(b"x")
    client = MagicMock()
    client.post.return_value.json.return_value = {"asset_id": "ast_a"}
    from src.processing.originals import Folder

    return dict(client=client, library_id="lib_1", originals=Folder(tmp_path),
                f={"rel_path": "a.file", "file_size": 1, "media_type": media_type, "content_hash": SHA},
                proxy_cache=MagicMock(), stats=ScanStats(), progress=MagicMock(), task_id=1, counter_field="new")


def test_a_scanned_image_says_how_its_proxy_was_made(tmp_path):
    import json
    from unittest.mock import patch

    from src.processing import scan

    args = _scan_args(tmp_path, "image")
    with (
        patch.object(scan, "_generate_proxy_bytes", return_value=(b"jpeg", 10, 10)),
        patch.object(scan, "_build_exif_payload", return_value={}),
        patch.object(scan, "_jpeg_to_webp", return_value=b"webp"),
    ):
        scan._scan_one(**args)
    [call] = [c for c in args["client"].post.call_args_list if c.args[0] == "/v1/ingest"]
    # Each by its name (ADR-024 phase 1 part 2: a server takes them from part 1), the preview too.
    assert json.loads(call.kwargs["data"]["lineage"]) == {
        "lumiverb.preview": P.lineage("lumiverb.preview", P.effective_settings("lumiverb.preview"), SHA),
        "lumiverb.capture_time": P.lineage("lumiverb.capture_time", {}, SHA),
        "lumiverb.live_photo": P.lineage("lumiverb.live_photo", {}, SHA)}
    assert set(call.kwargs["files"]) == {"lumiverb.preview"}


def test_a_scanned_video_says_how_its_proxy_probe_and_preview_were_made(tmp_path):
    import json
    from unittest.mock import patch

    from src.processing import scan

    args = _scan_args(tmp_path, "video")
    facet = MagicMock()
    facet.to_dict.return_value = {"frame_rate": 25.0}
    with (
        patch.object(scan, "_extract_video_poster", return_value=(b"jpeg", 10, 10)),
        patch.object(scan, "_build_exif_payload", return_value={}),
        patch.object(scan, "_generate_video_preview", return_value=b"mp4"),
        patch.object(scan, "_jpeg_to_webp", return_value=b"webp"),
        patch.object(scan, "probe_video", return_value=facet),
    ):
        scan._scan_one_video(**args)
    posts = {c.args[0]: c for c in args["client"].post.call_args_list}
    assert json.loads(posts["/v1/ingest"].kwargs["data"]["lineage"]) == {
        "lumiverb.preview": P.lineage("lumiverb.preview", P.effective_settings("lumiverb.preview"), SHA),
        "lumiverb.capture_time": P.lineage("lumiverb.capture_time", {}, SHA),
        "lumiverb.live_photo": P.lineage("lumiverb.live_photo", {}, SHA),
        "lumiverb.video.format": P.lineage("lumiverb.video.format", {}, SHA)}
    assert json.loads(posts["/v1/assets/ast_a/artifacts/lumiverb.video.preview"].kwargs["data"]["lineage"]) == P.lineage(
        "lumiverb.video.preview", P.effective_settings("lumiverb.video.preview"), SHA)


def test_a_failed_probe_isnt_claimed(tmp_path):
    import json
    from unittest.mock import patch

    from src.processing import scan

    args = _scan_args(tmp_path, "video")
    with (
        patch.object(scan, "_extract_video_poster", return_value=(b"jpeg", 10, 10)),
        patch.object(scan, "_build_exif_payload", return_value={}),
        patch.object(scan, "_generate_video_preview", return_value=None),
        patch.object(scan, "_jpeg_to_webp", return_value=b"webp"),
        patch.object(scan, "probe_video", side_effect=RuntimeError("ffprobe")),
    ):
        scan._scan_one_video(**args)
    [call] = [c for c in args["client"].post.call_args_list if c.args[0] == "/v1/ingest"]
    assert set(json.loads(call.kwargs["data"]["lineage"])) == {"lumiverb.preview", "lumiverb.capture_time",
                                                               "lumiverb.live_photo"}


def test_a_refresh_swaps_the_settings_in_at_once_and_keeps_them_when_the_server_cant_say():
    """The scheduler refreshes while jobs run: none may see the registry's
    defaults in between, nor after the server fails to answer."""
    import threading

    from src.processing.producer_settings import ProducerSettings

    client = _client({"lumiverb.image.description": {"model": "custom", "max_edge": 999}})
    producers = ProducerSettings(client)
    assert producers.settings("lumiverb.image.description")["max_edge"] == 999
    seen: list[int] = []
    answered = threading.Event()

    def slow_get(*args, **kwargs):
        seen.append(producers.settings("lumiverb.image.description")["max_edge"])  # what a job reads meanwhile
        answered.set()
        raise ConnectionError("the API is restarting")

    client.get.side_effect = slow_get
    assert producers.refresh() is False
    assert seen == [999] and producers.settings("lumiverb.image.description")["max_edge"] == 999
