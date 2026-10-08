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
    from src.client.proxy import proxy_gen
    from src.client.video import scene_segmenter, video_scanner
    from src.client.video.analysis_proxy import AnalysisProxySettings
    from src.client.workers.embeddings import clip_provider
    from src.client.workers.faces import insightface_provider as face

    proxy = P.PRODUCERS["proxy"].defaults
    assert (proxy_gen.PROXY_LONG_EDGE, proxy_gen.PROXY_JPEG_QUALITY) == (proxy["long_edge"], proxy["jpeg_quality"])
    assert AnalysisProxySettings().output() == dict(P.PRODUCERS["analysis_proxy"].defaults)
    scenes = P.PRODUCERS["scenes"].defaults
    assert video_scanner.OUT_WIDTH == scenes["frame_width"]
    assert (scene_segmenter.PHASH_THRESHOLD, scene_segmenter.PHASH_HASH_SIZE) == (
        scenes["phash_threshold"], scenes["phash_hash_size"])
    assert (scene_segmenter.TEMPORAL_CEILING_SEC, scene_segmenter.DEBOUNCE_SEC) == (
        scenes["temporal_ceiling_sec"], scenes["debounce_sec"])
    assert "select='eq(pict_type\\\\,I)'" in inspect.getsource(video_scanner) and scenes["frames"] == "keyframes"
    faces = P.PRODUCERS["faces"].defaults
    assert face.MODEL_VERSION == faces["model"]
    assert face.InsightFaceProvider._MAX_DETECT_EDGE == faces["max_detect_edge"]
    assert f"det_size=({faces['det_size']}, {faces['det_size']})" in inspect.getsource(face)
    assert (face.MIN_DETECTION_CONFIDENCE, face.MIN_BBOX_AREA_FRACTION, face.MIN_FACE_PIXELS,
            face.MIN_RELATIVE_SIZE, face.MIN_LAPLACIAN_VARIANCE) == (
        faces["min_confidence"], faces["min_area_fraction"], faces["min_face_pixels"],
        faces["min_relative_size"], faces["min_sharpness"])
    clip = P.PRODUCERS["clip"].defaults
    assert clip_provider.MODEL_VERSION == f"{clip['model']}-{clip['pretrained']}"
    from src.client.cli import repair
    from src.client.proxy import proxy_cache

    assert repair.PROXY_CACHE_EDGE == proxy_cache._DEFAULT_MAX_EDGE == clip["input_edge"]
    # What changes the output lives only on the server: the worker's config has none of it.
    assert not {"whisper_model", "proxy_max_edge", "analysis_proxy_max_edge", "vision_api_url",
                "vision_api_key", "vision_model_id"} & set(cli_config.CLIConfig.model_fields)


def test_the_previews_settings_are_what_the_scan_renders():
    import inspect

    from src.client.cli import ingest

    source = inspect.getsource(ingest._generate_video_preview)
    preview = P.PRODUCERS["video_preview"].defaults
    assert f"PREVIEW_DURATION_SEC = {preview['seconds']}" in source
    assert f"PREVIEW_MAX_HEIGHT = {preview['max_height']}" in source
    assert f'"-crf", "{preview["crf"]}"' in source
    assert f'"-b:a", "{preview["audio_kbps"]}k"' in source
    assert ingest.PROXY_WEBP_QUALITY == P.PRODUCERS["proxy"].defaults["webp_quality"]
    from src.server.api.routers import ingest as server_ingest
    assert server_ingest.THUMBNAIL_LONG_EDGE == P.PRODUCERS["proxy"].defaults["thumbnail_edge"]


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
    from src.client.cli.producer_settings import ProducerSettings

    ps = ProducerSettings(_client({"transcript": {"model": "medium"}}))
    assert ps.settings("transcript")["model"] == "medium"
    lin = ps.lineage("transcript", "ab" * 32)
    assert lin == {"producer": "whisper", "version": "1",
                   "settings_hash": P.settings_hash({**P.effective_settings("transcript"), "model": "medium"}),
                   "source_sha256": "ab" * 32}


def test_an_older_server_means_the_registrys_settings():
    from src.client.cli.producer_settings import ProducerSettings

    ps = ProducerSettings(_client(fails=True))
    assert ps.settings("transcript") == P.effective_settings("transcript")


def test_settings_actually_used_are_what_lineage_hashes():
    from src.client.cli.producer_settings import ProducerSettings

    ps = ProducerSettings(_client())
    used = {**ps.settings("clip"), "input_edge": 2048}
    assert ps.lineage("clip", None, used=used)["settings_hash"] == P.settings_hash(used)
    assert ps.lineage("clip", None, used=used)["settings_hash"] != ps.lineage("clip", None)["settings_hash"]


# ---------------------------------------------------------------------------
# The scan says how it made each clip's proxy, probe and preview.
# ---------------------------------------------------------------------------

SHA = "cd" * 32


def _scan_args(tmp_path, media_type: str) -> dict:
    from src.client.cli.scan import ScanStats

    (tmp_path / "a.file").write_bytes(b"x")
    client = MagicMock()
    client.post.return_value.json.return_value = {"asset_id": "ast_a"}
    return dict(client=client, library_id="lib_1", root_path=tmp_path,
                f={"rel_path": "a.file", "file_size": 1, "media_type": media_type, "source_sha256": SHA},
                proxy_cache=MagicMock(), stats=ScanStats(), progress=MagicMock(), task_id=1, counter_field="new")


def test_a_scanned_image_says_how_its_proxy_was_made(tmp_path):
    import json
    from unittest.mock import patch

    from src.client.cli import scan

    args = _scan_args(tmp_path, "image")
    with (
        patch.object(scan, "_generate_proxy_bytes", return_value=(b"jpeg", 10, 10)),
        patch.object(scan, "_build_exif_payload", return_value={}),
        patch.object(scan, "_jpeg_to_webp", return_value=b"webp"),
    ):
        scan._scan_one(**args)
    [call] = [c for c in args["client"].post.call_args_list if c.args[0] == "/v1/ingest"]
    assert json.loads(call.kwargs["data"]["lineage"]) == {
        "proxy": P.lineage("proxy", P.effective_settings("proxy"), SHA)}


def test_a_scanned_video_says_how_its_proxy_probe_and_preview_were_made(tmp_path):
    import json
    from unittest.mock import patch

    from src.client.cli import scan

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
        "proxy": P.lineage("proxy", P.effective_settings("proxy"), SHA),
        "probe": P.lineage("probe", {}, SHA)}
    assert json.loads(posts["/v1/assets/ast_a/artifacts/video_preview"].kwargs["data"]["lineage"]) == P.lineage(
        "video_preview", P.effective_settings("video_preview"), SHA)


def test_a_failed_probe_isnt_claimed(tmp_path):
    import json
    from unittest.mock import patch

    from src.client.cli import scan

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
    assert set(json.loads(call.kwargs["data"]["lineage"])) == {"proxy"}


def test_descriptions_say_which_file_each_came_from(tmp_path, monkeypatch):
    from unittest.mock import patch

    from rich.console import Console

    from src.client.cli import ingest
    from src.client.cli.producer_settings import ProducerSettings

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    model = "qwen3-vl:8b"
    client = _client({a: {"model": model} for a in ("vision", "ocr", "scene_vision")})
    producers = ProducerSettings(client)
    client.get.return_value.json.return_value = {"items": [{"asset_id": "ast_a", "rel_path": "a.jpg", "sha256": SHA}]}
    with (
        patch.object(ingest, "_resolve_vision_config", return_value=("http://vision", None, model, "account settings")),
        patch("src.client.workers.captions.factory.get_caption_provider") as provider,
        patch.object(ingest, "_backfill_one", return_value={"asset_id": "ast_a", "model_id": model,
                                                            "description": "a dog", "tags": []}),
    ):
        ingest.run_backfill_vision(client, {"library_id": "lib_1", "name": "L", "root_path": str(tmp_path)},
                                   console=Console(quiet=True), producers=producers)
    assert provider.call_args.args[0] == model
    [call] = [c for c in client.post.call_args_list if c.args[0] == "/v1/assets/batch-vision"]
    body = call.kwargs["json"]
    assert body["items"][0]["source_sha256"] == SHA and "lineage" not in body["items"][0]
    assert body["lineage"] == P.lineage("vision", P.effective_settings("vision", account={"model": model}), None)
