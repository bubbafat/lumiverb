"""Enrichment reads analysis proxies, not originals (ADR-016 phase 2).

`render` makes a proxy for each video while its storage is reachable.
Transcription, scene detection and scene vision then read the proxy, so
they keep going while the storage holding the originals sleeps. Probing
and rendering wait for it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from src.client.cli.config import CLIConfig, save_config
from src.client.cli.repair import run_repair

MAC = "/Volumes/media-01"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", lambda: h)
    return h


@pytest.fixture
def library(home: Path, tmp_path: Path) -> dict:
    mount = tmp_path / "mnt"
    (mount / "Footage").mkdir(parents=True)
    (mount / "Footage" / "a.mov").write_bytes(b"original a")
    save_config(CLIConfig(root_map={MAC: str(mount)}))
    return {"library_id": "lib_1", "name": "Footage", "root_path": f"{MAC}/Footage"}


@pytest.fixture
def asleep(home: Path) -> dict:
    """A library whose storage isn't reachable from here right now."""
    save_config(CLIConfig(root_map={MAC: str(home / "not-mounted")}))
    return {"library_id": "lib_1", "name": "Footage", "root_path": f"{MAC}/Footage"}


def _cached(home: Path, asset_id: str, body: bytes = b"proxy") -> Path:
    path = home / ".cache" / "lumiverb" / "analysis" / f"{asset_id}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _run(client: MagicMock, library: dict, job_type: str, summary: dict, pages: dict[str, list[dict]], **kwargs):
    def page_missing(_client, _library_id, **flags):
        [flag] = [k for k, v in flags.items() if v]
        return pages.get(flag, [])

    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": 2, **summary}),
        patch("src.client.cli.repair._page_missing", side_effect=page_missing),
    ):
        run_repair(client, library, job_type=job_type, console=Console(quiet=True), **kwargs)


def _posts(client: MagicMock, suffix: str) -> list[str]:
    return [c.args[0] for c in client.post.call_args_list if c.args and c.args[0].endswith(suffix)]


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_render_is_an_enrich_job() -> None:
    from src.client.cli.main import ENRICH_TYPES
    from src.client.cli.repair import REPAIR_TYPES

    assert "render" in ENRICH_TYPES and "render" in REPAIR_TYPES


@pytest.mark.fast
def test_render_makes_uploads_and_caches_a_proxy(home: Path, library: dict) -> None:
    client = MagicMock()
    rendered_from: list[Path] = []

    def fake_render(source: Path, dest: Path, settings=None, *, timeout=None) -> None:
        rendered_from.append(source)
        dest.write_bytes(b"rendered from " + source.read_bytes())

    pages = {"missing_analysis_proxy": [
        {"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0},
        {"asset_id": "ast_b", "rel_path": "b.mov", "duration_sec": 4.0},  # not on disk
    ]}
    with patch("src.client.cli.repair.render_analysis_proxy", side_effect=fake_render):
        _run(client, library, "render", {"missing_analysis_proxy": 2}, pages)

    assert [p.name for p in rendered_from] == ["a.mov"]
    assert _posts(client, "/artifacts/analysis_proxy") == ["/v1/assets/ast_a/artifacts/analysis_proxy"]
    cache = home / ".cache" / "lumiverb" / "analysis"
    assert (cache / "ast_a.mp4").read_bytes() == b"rendered from original a"
    assert sorted(p.name for p in cache.iterdir()) == ["ast_a.mp4"]


@pytest.mark.fast
def test_render_waits_while_storage_sleeps(home: Path, asleep: dict) -> None:
    client = MagicMock()
    pages = {"missing_analysis_proxy": [{"asset_id": "ast_a", "rel_path": "a.mov"}]}
    with patch("src.client.cli.repair.render_analysis_proxy") as render:
        _run(client, asleep, "render", {"missing_analysis_proxy": 1}, pages)
    render.assert_not_called()
    assert client.post.call_count == 0


@pytest.mark.fast
def test_a_failed_render_uploads_nothing_and_leaves_nothing(home: Path, library: dict) -> None:
    from src.client.video.analysis_proxy import RenderError

    client = MagicMock()
    pages = {"missing_analysis_proxy": [{"asset_id": "ast_a", "rel_path": "a.mov"}]}
    with patch("src.client.cli.repair.render_analysis_proxy", side_effect=RenderError("bad file")):
        _run(client, library, "render", {"missing_analysis_proxy": 1}, pages)
    assert _posts(client, "/artifacts/analysis_proxy") == []
    cache = home / ".cache" / "lumiverb" / "analysis"
    assert not cache.exists() or list(cache.iterdir()) == []


@pytest.mark.fast
def test_a_failed_upload_keeps_nothing_in_the_cache(home: Path, library: dict) -> None:
    client = MagicMock()
    client.post.side_effect = RuntimeError("server went away")

    def fake_render(source: Path, dest: Path, settings=None, *, timeout=None) -> None:
        dest.write_bytes(b"proxy")

    pages = {"missing_analysis_proxy": [{"asset_id": "ast_a", "rel_path": "a.mov"}]}
    with patch("src.client.cli.repair.render_analysis_proxy", side_effect=fake_render):
        _run(client, library, "render", {"missing_analysis_proxy": 1}, pages)
    cache = home / ".cache" / "lumiverb" / "analysis"
    assert not cache.exists() or list(cache.iterdir()) == []


# ---------------------------------------------------------------------------
# transcription, scenes and scene vision read the proxy
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_transcription_reads_the_proxy_while_storage_sleeps(home: Path, asleep: dict) -> None:
    proxy = _cached(home, "ast_a")
    client = MagicMock()
    pages = {"missing_transcription": [
        {"asset_id": "ast_a", "rel_path": "a.mov", "has_analysis_proxy": True},
        {"asset_id": "ast_b", "rel_path": "b.mov", "has_analysis_proxy": False},
    ]}
    with patch("src.client.cli.repair._transcribe_one", return_value=("1\n00:00:00,000 --> 00:00:01,000\nhi\n", "en")) as tr:
        _run(client, asleep, "transcribe", {"missing_transcription": 2}, pages)

    assert [c.args[0] for c in tr.call_args_list] == [proxy]
    assert _posts(client, "/transcript") == ["/v1/assets/ast_a/transcript"]


@pytest.mark.fast
def test_transcription_downloads_a_proxy_it_does_not_have(home: Path, asleep: dict) -> None:
    from contextlib import contextmanager

    client = MagicMock()

    @contextmanager
    def stream(path: str, **kw):
        resp = MagicMock(status_code=200)
        resp.iter_bytes.return_value = [b"downloaded"]
        yield resp

    client.stream.side_effect = stream
    pages = {"missing_transcription": [{"asset_id": "ast_a", "rel_path": "a.mov", "has_analysis_proxy": True}]}
    with patch("src.client.cli.repair._transcribe_one", return_value=("", "")) as tr:
        _run(client, asleep, "transcribe", {"missing_transcription": 1}, pages)
    [call] = tr.call_args_list
    assert call.args[0].read_bytes() == b"downloaded"


@pytest.mark.fast
def test_scene_detection_reads_the_proxy_while_storage_sleeps(home: Path, asleep: dict) -> None:
    proxy = _cached(home, "ast_a")
    client = MagicMock()
    pages = {"missing_video_scenes": [
        {"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0, "has_analysis_proxy": True},
        {"asset_id": "ast_b", "rel_path": "b.mov", "duration_sec": 4.0, "has_analysis_proxy": False},
    ]}
    with patch("src.client.cli.video_index.index_video_scenes",
               return_value={"scenes": 1, "chunks": 1, "elapsed": 0.1}) as idx:
        _run(client, asleep, "video-scenes", {"missing_video_scenes": 2}, pages)
    assert [c.kwargs["source_path"] for c in idx.call_args_list] == [proxy]


@pytest.mark.fast
def test_scene_vision_reads_the_proxy_while_storage_sleeps(home: Path, asleep: dict) -> None:
    proxy = _cached(home, "ast_a")
    client = MagicMock()
    pages = {"missing_scene_vision": [
        {"asset_id": "ast_a", "rel_path": "a.mov", "has_analysis_proxy": True},
        {"asset_id": "ast_b", "rel_path": "b.mov", "has_analysis_proxy": False},
    ]}
    with (
        patch("src.client.cli.ingest._resolve_vision_config", return_value=(None, None, None, "none")),
        patch("src.client.cli.video_index.enrich_video_scenes",
              return_value={"enriched": 1, "skipped": 0, "failed": 0, "elapsed": 0.1}) as enr,
    ):
        _run(client, asleep, "scene-vision", {"missing_scene_vision": 2}, pages)
    assert [c.kwargs["source_path"] for c in enr.call_args_list] == [proxy]


@pytest.mark.fast
def test_all_renders_before_reading_proxies(home: Path, library: dict) -> None:
    order: list[str] = []
    client = MagicMock()

    def page_missing(_client, _library_id, **flags):
        [flag] = [k for k, v in flags.items() if v]
        order.append(flag)
        return []

    summary = {"total_assets": 1, "missing_probe": 1, "missing_analysis_proxy": 1, "missing_transcription": 1,
               "missing_video_scenes": 1, "missing_scene_vision": 1}
    with (
        patch("src.client.cli.repair.get_repair_summary", return_value=summary),
        patch("src.client.cli.repair._page_missing", side_effect=page_missing),
        patch("src.client.cli.ingest._resolve_vision_config", return_value=(None, None, None, "none")),
    ):
        run_repair(client, library, job_type="all", console=Console(quiet=True))
    assert order.index("missing_probe") < order.index("missing_analysis_proxy") < order.index("missing_transcription")
    assert order.index("missing_analysis_proxy") < order.index("missing_video_scenes")


@pytest.mark.fast
def test_scene_steps_count_what_they_did(home: Path, asleep: dict) -> None:
    _cached(home, "ast_a")
    client = MagicMock()
    pages = {"missing_video_scenes": [
        {"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0, "has_analysis_proxy": True},
    ]}
    console = Console(record=True, width=200)

    def page_missing(_client, _library_id, **flags):
        [flag] = [k for k, v in flags.items() if v]
        return pages.get(flag, [])

    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": 1, "missing_video_scenes": 1}),
        patch("src.client.cli.repair._page_missing", side_effect=page_missing),
        patch("src.client.cli.video_index.index_video_scenes", return_value={"scenes": 1, "chunks": 1, "elapsed": 0.1}),
    ):
        run_repair(client, asleep, job_type="video-scenes", console=console)
    assert "1 fixed" in console.export_text()


# ---------------------------------------------------------------------------
# The worker gives enrichment a time budget: told to stop, each step stops
# between items and no later step starts. Probing and rendering stop when
# the storage goes to sleep mid-step, instead of timing out on each file.
# ---------------------------------------------------------------------------

VISION = ("http://vision", None, "model", "test")

# job type, its missing_* flag, what handles one item, extra patches
STEPS = [
    ("probe", "missing_probe", "src.client.cli.repair._probe_one", {}),
    ("render", "missing_analysis_proxy", "src.client.cli.repair._render_one", {}),
    ("embed", "missing_embeddings", "src.client.cli.repair._repair_embed_one",
     {"src.client.workers.embeddings.clip_provider.CLIPEmbeddingProvider": MagicMock()}),
    ("ocr", "missing_ocr", "src.client.cli.repair._ocr_one",
     {"src.client.cli.ingest._resolve_vision_config": MagicMock(return_value=VISION),
      "src.client.workers.captions.factory.get_caption_provider": MagicMock()}),
    ("faces", "missing_faces", "src.client.cli.repair._run_face_pipeline", {}),
    ("transcribe", "missing_transcription", "src.client.cli.repair._transcribe_one", {}),
    ("video-scenes", "missing_video_scenes", "src.client.cli.video_index.index_video_scenes", {}),
    ("scene-vision", "missing_scene_vision", "src.client.cli.video_index.enrich_video_scenes",
     {"src.client.cli.ingest._resolve_vision_config": MagicMock(return_value=(None, None, None, "none"))}),
]


@pytest.mark.fast
@pytest.mark.parametrize(("job_type", "flag", "target", "extra"), STEPS, ids=[s[0] for s in STEPS])
def test_a_step_told_to_stop_does_no_more_items(home: Path, library: dict, job_type: str, flag: str,
                                                target: str, extra: dict) -> None:
    from contextlib import ExitStack

    _cached(home, "ast_a")
    stop = {"now": False}

    def page_missing(_client, _library_id, **flags):
        stop["now"] = True  # the step has started
        return [{"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0, "has_analysis_proxy": True}]

    with ExitStack() as stack:
        stack.enter_context(patch("src.client.cli.repair.get_repair_summary",
                                  return_value={"total_assets": 1, flag: 1}))
        stack.enter_context(patch("src.client.cli.repair._page_missing", side_effect=page_missing))
        for name, value in extra.items():
            stack.enter_context(patch(name, value))
        one = stack.enter_context(patch(target, return_value="ok"))
        run_repair(MagicMock(), library, job_type=job_type, console=Console(quiet=True),
                   should_stop=lambda: stop["now"])
    one.assert_not_called()


@pytest.mark.fast
def test_vision_told_to_stop_does_no_more_items(home: Path, library: dict) -> None:
    stop = {"now": False}
    client = MagicMock()

    def get(url: str, **kw):
        stop["now"] = True
        return MagicMock(**{"json.return_value": {"items": [{"asset_id": "ast_a", "rel_path": "a.jpg"}]}})

    client.get.side_effect = get
    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": 1, "missing_vision": 1}),
        patch("src.client.cli.ingest._resolve_vision_config", return_value=VISION),
        patch("src.client.workers.captions.factory.get_caption_provider"),
        patch("src.client.cli.ingest._backfill_one") as one,
    ):
        run_repair(client, library, job_type="vision", console=Console(quiet=True), should_stop=lambda: stop["now"])
    one.assert_not_called()


@pytest.mark.fast
def test_a_step_stops_between_items(home: Path, library: dict) -> None:
    done: list[str] = []
    pages = {"missing_analysis_proxy": [{"asset_id": f"ast_{x}", "rel_path": f"{x}.mov"} for x in "abc"]}
    with patch("src.client.cli.repair._render_one", side_effect=lambda *a: done.append(a[2]["asset_id"]) or "ok"):
        _run(MagicMock(), library, "render", {"missing_analysis_proxy": 3}, pages, should_stop=lambda: bool(done))
    assert done == ["ast_a"]


@pytest.mark.fast
def test_no_step_starts_once_told_to_stop(home: Path, library: dict) -> None:
    started: list[str] = []

    def page_missing(_client, _library_id, **flags):
        [flag] = [k for k, v in flags.items() if v]
        started.append(flag)
        return []

    with (
        patch("src.client.cli.repair.get_repair_summary",
              return_value={"total_assets": 1, "missing_probe": 1, "missing_analysis_proxy": 1}),
        patch("src.client.cli.repair._page_missing", side_effect=page_missing),
    ):
        run_repair(MagicMock(), library, job_type="all", console=Console(quiet=True), should_stop=lambda: bool(started))
    assert started == ["missing_probe"]


@pytest.mark.fast
@pytest.mark.parametrize(("job_type", "flag", "target"), [
    ("render", "missing_analysis_proxy", "src.client.cli.repair._render_one"),
    ("probe", "missing_probe", "src.client.cli.repair._probe_one"),
])
def test_a_step_stops_when_the_storage_goes_to_sleep(home: Path, library: dict, tmp_path: Path,
                                                     job_type: str, flag: str, target: str) -> None:
    # Otherwise each remaining file costs a CIFS timeout.
    tried: list[str] = []

    def one(*args):
        asset = args[2]
        tried.append(asset["asset_id"])
        if asset["asset_id"] == "ast_b":
            (tmp_path / "mnt").rename(tmp_path / "asleep")
            return "missing"
        return "ok"

    pages = {flag: [{"asset_id": f"ast_{x}", "rel_path": f"{x}.mov"} for x in "abcd"]}
    with patch(target, side_effect=one):
        _run(MagicMock(), library, job_type, {flag: 4}, pages)
    assert tried == ["ast_a", "ast_b"]


@pytest.mark.fast
def test_a_missing_file_alone_does_not_stop_the_step(home: Path, library: dict) -> None:
    tried: list[str] = []

    def one(*args):
        tried.append(args[2]["asset_id"])
        return "missing" if args[2]["asset_id"] == "ast_b" else "ok"

    pages = {"missing_analysis_proxy": [{"asset_id": f"ast_{x}", "rel_path": f"{x}.mov"} for x in "abc"]}
    with patch("src.client.cli.repair._render_one", side_effect=one):
        _run(MagicMock(), library, "render", {"missing_analysis_proxy": 3}, pages)
    assert tried == ["ast_a", "ast_b", "ast_c"]


# ---------------------------------------------------------------------------
# The worker leaves items that failed recently for later (skip_items), and
# hears of each item a step takes (on_take): an item that keeps failing
# mustn't sit first and use up every run's time.
# ---------------------------------------------------------------------------


@pytest.mark.fast
@pytest.mark.parametrize(("job_type", "flag", "target", "extra"), STEPS, ids=[s[0] for s in STEPS])
def test_a_step_leaves_skipped_items_and_reports_what_it_takes(home: Path, library: dict, job_type: str, flag: str,
                                                               target: str, extra: dict) -> None:
    from contextlib import ExitStack

    for asset_id in ("ast_a", "ast_b"):
        _cached(home, asset_id)
    page = [{"asset_id": x, "rel_path": f"{x}.mov", "duration_sec": 4.0, "has_analysis_proxy": True}
            for x in ("ast_a", "ast_b")]
    taken: list[tuple[str, str]] = []
    with ExitStack() as stack:
        stack.enter_context(patch("src.client.cli.repair.get_repair_summary",
                                  return_value={"total_assets": 2, flag: 2}))
        stack.enter_context(patch("src.client.cli.repair._page_missing", return_value=page))
        for name, value in extra.items():
            stack.enter_context(patch(name, value))
        # Embed and OCR batch what each item returns: a result for that clip.
        result = {"asset_id": "ast_b"} if job_type in ("embed", "ocr") else "ok"
        one = stack.enter_context(patch(target, return_value=result))
        run_repair(MagicMock(), library, job_type=job_type, console=Console(quiet=True),
                   should_stop=lambda: False, skip_items={(job_type, "ast_a")},
                   on_take=lambda step, asset_id: taken.append((step, asset_id)))
    assert taken == [(job_type, "ast_b")]
    assert one.call_count == 1


@pytest.mark.fast
def test_vision_leaves_skipped_items_and_reports_what_it_takes(home: Path, library: dict) -> None:
    client = MagicMock()
    client.get.return_value.json.return_value = {"items": [{"asset_id": x, "rel_path": f"{x}.jpg"}
                                                           for x in ("ast_a", "ast_b")]}
    taken: list[tuple[str, str]] = []
    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": 2, "missing_vision": 2}),
        patch("src.client.cli.ingest._resolve_vision_config", return_value=VISION),
        patch("src.client.workers.captions.factory.get_caption_provider"),
        patch("src.client.cli.ingest._backfill_one", return_value=None) as one,
    ):
        run_repair(client, library, job_type="vision", console=Console(quiet=True), skip_items={("vision", "ast_a")},
                   on_take=lambda step, asset_id: taken.append((step, asset_id)))
    assert taken == [("vision", "ast_b")]
    assert [c.kwargs["asset_id"] for c in one.call_args_list] == ["ast_b"]


# ---------------------------------------------------------------------------
# Lineage (ADR-016 phase 3): each step makes its artifact with the server's
# settings and says so, with the hash of the file it was made from.
# ---------------------------------------------------------------------------

SHA = "ab" * 32


def _with_producers(overrides: dict | None = None) -> MagicMock:
    """A client whose server says what each producer makes its artifact with."""
    from src.shared import producers as P

    listing = {"producers": [{"artifact": a, "settings": {**P.effective_settings(a), **(overrides or {}).get(a, {})}}
                             for a in P.ARTIFACTS]}
    client = MagicMock()

    def get(path, **_kwargs):
        resp = MagicMock()
        resp.json.return_value = listing if path == "/v1/producers" else {"items": []}
        return resp

    client.get.side_effect = get
    return client


def _sent(client: MagicMock, suffix: str) -> list:
    return [c for c in client.post.call_args_list if c.args and c.args[0].endswith(suffix)]


@pytest.mark.fast
def test_render_uses_the_servers_settings_and_says_so(home: Path, library: dict) -> None:
    import json

    from src.shared import producers as P

    client = _with_producers({"analysis_proxy": {"crf": 30}})
    used = []

    def fake_render(source: Path, dest: Path, settings=None, *, timeout=None) -> None:
        used.append(settings)
        dest.write_bytes(b"proxy")

    pages = {"missing_analysis_proxy": [{"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0, "sha256": SHA}]}
    with patch("src.client.cli.repair.render_analysis_proxy", side_effect=fake_render):
        _run(client, library, "render", {"missing_analysis_proxy": 1}, pages)

    assert used[0].crf == 30
    [call] = _sent(client, "/artifacts/analysis_proxy")
    want = {**P.effective_settings("analysis_proxy"), "crf": 30}
    assert json.loads(call.kwargs["data"]["lineage"]) == {
        "producer": "analysis-proxy", "version": "1", "settings_hash": P.settings_hash(want), "source_sha256": SHA}


@pytest.mark.fast
def test_probe_says_how_it_was_made(library: dict, tmp_path: Path) -> None:
    from src.client.cli.repair import _probe_one
    from src.client.cli.producer_settings import ProducerSettings
    from src.shared import producers as P

    client = _with_producers()
    facet = MagicMock()
    facet.to_dict.return_value = {"frame_rate": 25.0}
    with patch("src.client.cli.repair.probe_video", return_value=facet):
        assert _probe_one(client, tmp_path / "mnt" / "Footage", {"asset_id": "ast_a", "rel_path": "a.mov", "sha256": SHA},
                          ProducerSettings(client)) == "ok"
    body = client.put.call_args.kwargs["json"]
    assert body["frame_rate"] == 25.0
    assert body["lineage"] == P.lineage("probe", {}, SHA)


@pytest.mark.fast
def test_transcription_uses_the_servers_whisper_settings_and_says_so(home: Path, library: dict) -> None:
    from src.shared import producers as P

    _cached(home, "ast_a")
    client = _with_producers({"transcript": {"model": "medium", "vad_min_silence_ms": 700}})
    pages = {"missing_transcription": [{"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0, "sha256": SHA,
                                         "has_analysis_proxy": True}]}
    with patch("src.client.cli.repair._transcribe_one", return_value=("", "")) as tr:
        _run(client, library, "transcribe", {"missing_transcription": 1}, pages)

    assert tr.call_args.args[1:] == ("medium", 700)
    [call] = _sent(client, "/transcript")
    assert call.kwargs["json"]["lineage"] == P.lineage(
        "transcript", {**P.effective_settings("transcript"), "model": "medium", "vad_min_silence_ms": 700}, SHA)


@pytest.mark.fast
def test_embeddings_say_which_file_each_came_from_and_what_clip_saw(home: Path, library: dict) -> None:
    from src.shared import producers as P

    client = _with_producers()
    pages = {"missing_embeddings": [{"asset_id": "ast_a", "rel_path": "a.jpg", "sha256": SHA}]}
    with (
        patch("src.client.workers.embeddings.clip_provider.CLIPEmbeddingProvider") as clip,
        patch("src.client.cli.repair._repair_embed_one",
              return_value={"asset_id": "ast_a", "model_id": "clip", "model_version": "x", "vector": [0.1]}),
    ):
        _run(client, library, "embed", {"missing_embeddings": 1}, pages)

    clip.assert_called_once_with(model_name="ViT-B-32", pretrained="openai")
    [call] = _sent(client, "/batch-embeddings")
    body = call.kwargs["json"]
    assert body["items"][0]["source_sha256"] == SHA and "lineage" not in body["items"][0]
    used = {**P.effective_settings("clip"), "input_edge": CLIConfig().proxy_max_edge}
    assert body["lineage"] == P.lineage("clip", used, None)


@pytest.mark.fast
def test_ocr_says_which_file_each_came_from(home: Path, library: dict) -> None:
    from src.shared import producers as P

    client = _with_producers({a: {"model": "qwen3-vl:8b"} for a in ("vision", "ocr", "scene_vision")})
    pages = {"missing_ocr": [{"asset_id": "ast_a", "rel_path": "a.jpg", "sha256": SHA}]}
    with (
        patch("src.client.cli.ingest._resolve_vision_config", return_value=VISION),
        patch("src.client.workers.captions.factory.get_caption_provider") as provider,
        patch("src.client.cli.repair._ocr_one", return_value={"asset_id": "ast_a", "ocr_text": "EXIT"}),
    ):
        _run(client, library, "ocr", {"missing_ocr": 1}, pages)

    assert provider.call_args.args[0] == "qwen3-vl:8b"
    assert provider.call_args.kwargs["ocr_settings"]["prompt"] == P.OCR_PROMPT
    [call] = _sent(client, "/batch-ocr")
    body = call.kwargs["json"]
    assert body["items"] == [{"asset_id": "ast_a", "ocr_text": "EXIT", "source_sha256": SHA}]
    assert body["lineage"] == P.lineage("ocr", P.effective_settings("ocr", account={"model": "qwen3-vl:8b"}), None)


@pytest.mark.fast
def test_scenes_say_how_they_were_found(home: Path, library: dict) -> None:
    from src.shared import producers as P

    _cached(home, "ast_a")
    client = _with_producers()
    pages = {"missing_video_scenes": [{"asset_id": "ast_a", "rel_path": "a.mov", "duration_sec": 4.0, "sha256": SHA,
                                         "has_analysis_proxy": True}]}
    with patch("src.client.cli.video_index.index_video_scenes",
               return_value={"scenes": 1, "chunks": 1, "elapsed": 0.1}) as index:
        _run(client, library, "video-scenes", {"missing_video_scenes": 1}, pages)

    assert index.call_args.kwargs["lineage"] == P.lineage("scenes", P.effective_settings("scenes"), SHA)
