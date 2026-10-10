"""Running one job of a producer's (ADR-016 phase 4).

Every producer's work goes through one runner (src/producers/runner.py):
the producer makes a clip's artifact and saves it; the runner owns
stopping, saving (no NUL), and whose an error is (#70: the clip's is
charged now, the API or its database away charges nothing, anything else
is a crash, counted). The AI machines' trouble goes through their guard,
which charges no clip for it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.processing.api import LumiverbAPIError
from src.processing.machine import Machine
from src.processing.producer_settings import ProducerSettings
from src.producers import runner
from src.producers.runner import NOT_TRIED, Crashed, run
from src.server.scheduler import runners
from src.server.scheduler.dispatch import Job

SHA = "a" * 64

pytestmark = pytest.mark.fast


class FakeAccount:
    def __init__(self, tmp_path: Path) -> None:
        self.tenant_id = "t1"
        self.client = MagicMock()
        self.client.base_url, self.client.token = "http://api", "lv_key"
        self.producers = ProducerSettings(None)
        self.failures = MagicMock()
        self.vision = MagicMock()
        self.vision.model = "qwen3-vl:8b-instruct"
        self.vision.charges.return_value = True
        self.transcripts = MagicMock()
        self.transcripts.model = "small"
        self.transcripts.charges.return_value = True
        self.analysis_cache = MagicMock()
        self.analysis_cache.path_for.side_effect = lambda asset_id: tmp_path / "analysis" / f"{asset_id}.mp4"
        self.machine = Machine(analysis_proxy_decoder="cpu", gpu_decodes=0)
        self.roots = {"lib_1": tmp_path}
        self.cache = MagicMock()
        self.cache.path = tmp_path / "proxies"
        self.models = MagicMock()
        self.stopping = threading.Event()
        self.gone: list[str] = []

    def guard(self, job: str):
        return {"vision": self.vision, "transcripts": self.transcripts}[job]

    def root(self, library_id: str):
        return self.roots.get(library_id)

    def unreachable(self, library_id: str) -> None:
        self.gone.append(library_id)
        self.roots[library_id] = None

    def storage_gone(self, library_id: str) -> bool:
        """As the account looks (an empty or missing folder is gone)."""
        root = self.roots.get(library_id)
        return root is None or not root.is_dir() or not any(root.iterdir())

    def proxy_cache(self, library_id: str):
        return self.cache

    def vision_provider(self):
        return "provider", self.vision.model

    def transcriber(self):
        return "transcriber"


def _job(kind: str, *ids: str, **extra) -> Job:
    return Job("t1", kind, 3, tuple({"asset_id": i, "library_id": "lib_1", "rel_path": f"{i}.jpg", "sha256": SHA,
                                     **extra} for i in ids))


def _run(acct: FakeAccount, kind: str, *ids: str, **extra):
    """What the scheduler runs for the kind (its producer's work, through the runner)."""
    return runners.runners()[kind](acct, _job(kind, *ids, **extra))


@pytest.fixture
def acct(tmp_path: Path) -> FakeAccount:
    return FakeAccount(tmp_path)


def _posted(acct: FakeAccount) -> dict[str, dict]:
    return {c.args[0]: c.kwargs.get("json") for c in acct.client.post.call_args_list}


def _item_posted(acct: FakeAccount, route: str) -> tuple[dict, dict]:
    """(the one item, the batch's lineage) a batch route was sent."""
    body = _posted(acct)[route]
    [item] = body["items"]
    return item, body["lineage"]


def _charged(acct: FakeAccount) -> list[tuple[str, str]]:
    return [c.args[:2] for c in acct.failures.add.call_args_list]


# ---------------------------------------------------------------------------
# Each producer's work: made, and saved with its lineage
# ---------------------------------------------------------------------------


def test_a_description_is_saved_with_its_lineage(acct, monkeypatch) -> None:
    monkeypatch.setattr("src.producers.vision.work.describe_clip", lambda **kw: {
        "asset_id": kw["asset_id"], "model_id": kw["model"], "model_version": "1",
        "description": "a dog on a beach", "tags": ["dog"]})
    assert _run(acct, "vision", "ast_1") is None
    # The batch route, one clip: search is left to the upkeep sweep, not committed per clip.
    item, lineage = _item_posted(acct, "/v1/assets/batch-vision")
    assert item["description"] == "a dog on a beach" and item["asset_id"] == "ast_1"
    assert item["source_sha256"] == SHA
    assert lineage == acct.producers.lineage("vision", None,
                                             used=acct.producers.with_model("vision", acct.vision.model))


def test_a_description_that_fails_goes_through_the_machines_guard(acct, monkeypatch) -> None:
    error = RuntimeError("machine gone")
    monkeypatch.setattr("src.producers.vision.work.describe_clip", MagicMock(side_effect=error))
    acct.vision.charges.return_value = False  # the machines' trouble: it waits, uncharged
    assert _run(acct, "vision", "ast_1") == ["ast_1"]
    acct.vision.charges.assert_called_once_with(error)
    acct.failures.add.assert_not_called()
    acct.vision.charges.return_value = True  # the clip's: charged
    assert _run(acct, "vision", "ast_2") is None
    assert _charged(acct) == [("vision", "ast_2")]
    acct.client.post.assert_not_called()


def test_no_proxy_on_the_server_either_goes_to_the_guard(acct) -> None:
    """Not in the cache, and the server's says 404: nothing to describe, and the guard decides."""
    acct.cache.get.return_value = None
    acct.client.get.side_effect = LumiverbAPIError("not_found", "no proxy", 404)
    assert _run(acct, "vision", "ast_1") is None
    assert "no description" in str(acct.vision.charges.call_args.args[0])
    assert _charged(acct) == [("vision", "ast_1")]


@pytest.mark.parametrize("status, outcome", [(503, "waits"), (500, "crash")])
def test_the_servers_proxy_failing_otherwise_is_judged_by_whose(acct, status, outcome) -> None:
    acct.cache.get.return_value = None
    acct.client.get.side_effect = LumiverbAPIError("e", "x", status)
    if outcome == "crash":
        with pytest.raises(Crashed):
            _run(acct, "vision", "ast_1")
    else:
        assert _run(acct, "vision", "ast_1") == ["ast_1"]
    acct.vision.charges.assert_not_called()
    acct.failures.add.assert_not_called()


def test_text_in_an_image_is_saved_with_its_lineage(acct, monkeypatch) -> None:
    monkeypatch.setattr("src.producers.ocr.work.read_text", lambda **kw: {"asset_id": kw["asset_id"],
                                                                          "ocr_text": "STOP"})
    _run(acct, "ocr", "ast_1")
    body = _posted(acct)["/v1/assets/batch-ocr"]
    assert body["items"] == [{"asset_id": "ast_1", "ocr_text": "STOP", "source_sha256": SHA}]
    assert body["model_id"] == acct.vision.model and body["lineage"]["producer"] == "ocr"


def test_no_text_result_is_judged_by_the_guard(acct, monkeypatch) -> None:
    monkeypatch.setattr("src.producers.ocr.work.read_text", lambda **kw: None)
    acct.vision.charges.return_value = False
    assert _run(acct, "ocr", "ast_3") == ["ast_3"]
    assert "no OCR result" in str(acct.vision.charges.call_args.args[0])


def test_a_clip_embedding_is_saved_with_its_lineage(acct, monkeypatch) -> None:
    monkeypatch.setattr("src.producers.clip.work.embed", lambda **kw: {
        "asset_id": kw["asset_id"], "model_id": "clip", "model_version": "ViT-B-32", "vector": [0.1, 0.2]})
    _run(acct, "clip", "ast_1")
    item, lineage = _item_posted(acct, "/v1/assets/batch-embeddings")
    assert item["vector"] == [0.1, 0.2] and item["source_sha256"] == SHA
    assert lineage["producer"] == "clip"
    # The model is the scheduler's, as the account's settings say.
    acct.models.clip.assert_called_once_with("ViT-B-32", "openai")


def test_a_proxy_that_cant_be_read_is_the_clips(acct) -> None:
    acct.cache.get.return_value = b"not a jpeg"
    acct.models.clip.return_value = MagicMock(model_id="clip", model_version="v")
    assert _run(acct, "clip", "ast_1") is None
    assert _charged(acct) == [("clip", "ast_1")]


@pytest.mark.parametrize("kind", ["clip", "ocr"])
def test_this_machines_trouble_reading_a_proxy_is_a_crash(acct, kind) -> None:
    acct.models.clip.return_value = MagicMock(model_id="clip", model_version="v")
    acct.cache.get.side_effect = OSError(5, "Input/output error")
    with pytest.raises(Crashed):
        _run(acct, kind, "ast_1")
    acct.failures.add.assert_not_called()
    acct.vision.charges.assert_not_called()


def test_an_embedding_that_cannot_be_made_is_a_failure(acct, monkeypatch) -> None:
    monkeypatch.setattr("src.producers.clip.work.embed", lambda **kw: None)
    _run(acct, "clip", "ast_1")
    assert _charged(acct) == [("clip", "ast_1")]


def test_the_gpu_running_out_of_memory_charges_no_clip(acct, monkeypatch) -> None:
    def embed(**kw):
        if kw["asset_id"] == "ast_2":
            raise RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB")
        return {"asset_id": kw["asset_id"], "model_id": "clip", "model_version": "v", "vector": [0.1]}

    monkeypatch.setattr("src.producers.clip.work.embed", embed)
    assert _run(acct, "clip", "ast_1", "ast_2") == ["ast_2"]
    acct.failures.add.assert_not_called()


def test_a_probe_runs_on_the_librarys_storage_and_saves_the_facet(acct, monkeypatch, tmp_path) -> None:
    (tmp_path / "ast_1.jpg").write_bytes(b"x")
    seen = []
    monkeypatch.setattr("src.processing.video.probe.probe_video",
                        lambda source: seen.append(source) or MagicMock(to_dict=lambda: {"duration_sec": 2.0}))
    assert _run(acct, "probe", "ast_1") is None
    assert seen == [tmp_path / "ast_1.jpg"]
    body = acct.client.put.call_args.kwargs["json"]
    assert acct.client.put.call_args.args[0] == "/v1/assets/ast_1/video-facet"
    assert body["duration_sec"] == 2.0 and body["lineage"]["producer"] == "ffprobe"
    assert body["lineage"]["source_sha256"] == SHA


def test_a_probe_that_ffprobe_cant_read_is_the_clips(acct, monkeypatch, tmp_path) -> None:
    (tmp_path / "ast_1.jpg").write_bytes(b"x")
    monkeypatch.setattr("src.processing.video.probe.probe_video", MagicMock(side_effect=ValueError("invalid data")))
    assert _run(acct, "probe", "ast_1") is None
    assert _charged(acct) == [("probe", "ast_1")]


@pytest.mark.parametrize("error", [FileNotFoundError(2, "No such file or directory: 'ffprobe'"),
                                   OSError(5, "Input/output error")])
def test_a_probe_this_machine_cant_run_is_a_crash_not_the_clips(acct, monkeypatch, tmp_path, error) -> None:
    """Review: OSError was the clip's, so no ffprobe or the mount's EIO charged
    every video at once. It's a crash: counted, uncharged."""
    (tmp_path / "ast_1.jpg").write_bytes(b"x")
    monkeypatch.setattr("src.processing.video.probe.probe_video", MagicMock(side_effect=error))
    with pytest.raises(Crashed) as crashed:
        _run(acct, "probe", "ast_1")
    assert crashed.value.asset_ids == ["ast_1"]
    acct.failures.add.assert_not_called()


def test_a_probe_ffprobe_refuses_is_the_clips(acct, monkeypatch, tmp_path) -> None:
    import subprocess

    (tmp_path / "ast_1.jpg").write_bytes(b"x")
    monkeypatch.setattr("src.processing.video.probe.probe_video",
                        MagicMock(side_effect=subprocess.CalledProcessError(1, "ffprobe")))
    assert _run(acct, "probe", "ast_1") is None
    assert _charged(acct) == [("probe", "ast_1")]


def test_probing_waits_while_the_storage_is_away(acct, monkeypatch) -> None:
    probe = MagicMock()
    monkeypatch.setattr("src.processing.video.probe.probe_video", probe)
    acct.roots = {}
    assert _run(acct, "probe", "ast_1") == NOT_TRIED
    probe.assert_not_called()
    acct.failures.add.assert_not_called()
    assert acct.gone == ["lib_1"]  # storage work there waits for the next look


def test_a_file_gone_from_reachable_storage_waits_for_its_scan(acct, tmp_path) -> None:
    (tmp_path / "other.mov").write_bytes(b"x")  # the storage is there; this file isn't
    assert _run(acct, "probe", "ast_1") == ["ast_1"]
    assert _run(acct, "render", "ast_2") == ["ast_2"]
    assert acct.gone == []


def test_storage_that_went_away_since_the_last_look_isnt_tried(acct, tmp_path) -> None:
    # Review round 2: an unmounted share is an empty folder; every file in it
    # read as missing, held an hour each.
    assert _run(acct, "probe", "ast_1") == NOT_TRIED
    assert acct.gone == ["lib_1"]
    acct.roots = {"lib_1": tmp_path / "gone"}
    assert _run(acct, "render", "ast_2") == NOT_TRIED


def test_rendering_uses_the_servers_settings_and_this_machines_decoder(acct, monkeypatch, tmp_path) -> None:
    (tmp_path / "ast_1.jpg").write_bytes(b"x")
    used = []

    def render(source, dest, settings, *, timeout):
        used.append(settings)
        dest.write_bytes(b"mp4")

    monkeypatch.setattr("src.processing.video.analysis_proxy.render_analysis_proxy", render)
    assert _run(acct, "render", "ast_1") is None
    [settings] = used
    assert settings.decoder == "cpu" and settings.gpu_decodes == 0
    assert settings.max_edge == acct.producers.settings("analysis_proxy")["max_edge"]
    route = acct.client.post.call_args.args[0]
    assert route == "/v1/assets/ast_1/artifacts/analysis_proxy"
    assert '"producer": "analysis-proxy"' in acct.client.post.call_args.kwargs["data"]["lineage"]
    acct.analysis_cache.put.assert_called_once()


def test_a_render_that_fails_is_the_clips_and_leaves_nothing(acct, monkeypatch, tmp_path) -> None:
    from src.processing.video.analysis_proxy import RenderError

    (tmp_path / "ast_1.jpg").write_bytes(b"x")

    def render(source, dest, settings, *, timeout):
        dest.write_bytes(b"half")
        raise RenderError("ffmpeg exit 1")

    monkeypatch.setattr("src.processing.video.analysis_proxy.render_analysis_proxy", render)
    assert _run(acct, "render", "ast_1") is None
    assert _charged(acct) == [("analysis_proxy", "ast_1")]
    assert not list((tmp_path / "analysis").glob("*"))
    acct.client.post.assert_not_called()


def _renders(monkeypatch) -> None:
    def render(source, dest, settings, *, timeout):
        dest.write_bytes(b"mp4")

    monkeypatch.setattr("src.processing.video.analysis_proxy.render_analysis_proxy", render)


def test_an_upload_that_fails_leaves_nothing_in_the_cache(acct, monkeypatch, tmp_path) -> None:
    (tmp_path / "ast_1.jpg").write_bytes(b"x")
    _renders(monkeypatch)
    acct.client.post.side_effect = LumiverbAPIError("bad", "refused", 422)
    assert _run(acct, "render", "ast_1") is None
    assert _charged(acct) == [("analysis_proxy", "ast_1")]
    acct.analysis_cache.put.assert_not_called()
    assert not list((tmp_path / "analysis").glob("*"))  # the .rendering file is gone


def test_an_analysis_proxys_lineage_leaves_out_this_machines_way_of_rendering(acct, monkeypatch, tmp_path) -> None:
    import json

    from src.processing.video.analysis_proxy import AnalysisProxySettings

    (tmp_path / "ast_1.jpg").write_bytes(b"x")
    _renders(monkeypatch)
    hashes = []
    for encoder, decoder, gpus in (("libx264", "cpu", 0), ("h264_nvenc", "cuda", 2)):
        acct.machine = Machine(analysis_proxy_encoder=encoder, analysis_proxy_decoder=decoder, gpu_decodes=gpus)
        acct.client.post.reset_mock()
        _run(acct, "render", "ast_1")
        hashes.append(json.loads(acct.client.post.call_args.kwargs["data"]["lineage"])["settings_hash"])
    settings = AnalysisProxySettings.for_producer(acct.producers.settings("analysis_proxy"), "libx264")
    assert hashes == [acct.producers.lineage("analysis_proxy", SHA, used=settings.output())["settings_hash"]] * 2


def test_a_transcript_is_saved_with_its_lineage(acct, monkeypatch, tmp_path) -> None:
    acct.analysis_cache.get.return_value = tmp_path / "a.mp4"
    heard = MagicMock(return_value=("1\n00:00:00,000 --> 00:00:01,000\nhola\n", "es"))
    monkeypatch.setattr("src.producers.transcript.work.transcribe", heard)
    assert _run(acct, "transcript", "ast_1") is None
    assert heard.call_args.args == (tmp_path / "a.mp4", "transcriber", 500)
    body = _posted(acct)["/v1/assets/ast_1/transcript"]
    assert body["language"] == "es" and body["source"] == "whisper"
    assert body["lineage"]["producer"] == "whisper"


def test_a_transcripts_lineage_says_its_model(acct, monkeypatch, tmp_path) -> None:
    acct.analysis_cache.get.return_value = tmp_path / "a.mp4"
    monkeypatch.setattr("src.producers.transcript.work.transcribe", lambda *a: ("", ""))
    _run(acct, "transcript", "ast_1")
    lineage = _posted(acct)["/v1/assets/ast_1/transcript"]["lineage"]
    assert lineage == acct.producers.lineage("transcript", SHA, used=acct.producers.with_model("transcript", "small"))
    assert lineage != acct.producers.lineage("transcript", SHA,
                                             used=acct.producers.with_model("transcript", "large-v3"))


def test_this_machines_trouble_transcribing_is_a_crash(acct, monkeypatch, tmp_path) -> None:
    acct.analysis_cache.get.return_value = tmp_path / "a.mp4"
    monkeypatch.setattr("src.producers.transcript.work.transcribe",
                        MagicMock(side_effect=FileNotFoundError(2, "No such file or directory: 'ffmpeg'")))
    with pytest.raises(Crashed):
        _run(acct, "transcript", "ast_1")
    acct.failures.add.assert_not_called()


def test_the_machines_trouble_with_a_transcript_charges_no_clip(acct, monkeypatch, tmp_path) -> None:
    from src.processing.workers.transcripts.base import TranscriptError

    acct.analysis_cache.get.return_value = tmp_path / "a.mp4"
    error = TranscriptError("speaches broke off", endpoint_fault=True)
    monkeypatch.setattr("src.producers.transcript.work.transcribe", MagicMock(side_effect=error))
    acct.transcripts.charges.return_value = False
    assert _run(acct, "transcript", "ast_1") == ["ast_1"]
    acct.transcripts.charges.assert_called_once_with(error)
    acct.failures.add.assert_not_called()


def test_a_transcript_that_could_not_be_tried_is_a_failure(acct, monkeypatch, tmp_path) -> None:
    acct.analysis_cache.get.return_value = tmp_path / "a.mp4"
    monkeypatch.setattr("src.producers.transcript.work.transcribe", lambda *a: None)
    _run(acct, "transcript", "ast_1")
    assert acct.failures.add.call_args.args == ("transcript", "ast_1", "transcription failed (see the log)")


def test_a_transcript_whose_analysis_copy_cant_be_read_is_a_failure(acct) -> None:
    acct.analysis_cache.get.return_value = None
    _run(acct, "transcript", "ast_1")
    assert _charged(acct) == [("transcript", "ast_1")]


# ---------------------------------------------------------------------------
# Saving: once stopping nothing more; whose an error is, one rule
# ---------------------------------------------------------------------------


def test_once_stopping_nothing_more_is_made_or_saved(acct, monkeypatch, tmp_path) -> None:
    # A stop (an update, a reboot) kills ffmpeg mid-clip; what a job made of
    # that mustn't be saved as the clip's (an empty transcript, for good).
    describe = MagicMock()
    monkeypatch.setattr("src.producers.vision.work.describe_clip", describe)
    acct.stopping.set()
    assert _run(acct, "vision", "ast_1") == NOT_TRIED
    assert _run(acct, "transcript", "ast_2") == NOT_TRIED
    assert _run(acct, "probe", "ast_3") == NOT_TRIED
    describe.assert_not_called()
    acct.client.post.assert_not_called()


def test_probe_and_render_save_nothing_once_stopping(acct, monkeypatch, tmp_path) -> None:
    """Review (Oct 9): probe and render saved after the scheduler said stop."""
    (tmp_path / "ast_1.jpg").write_bytes(b"x")

    def probe_video(source):
        acct.stopping.set()  # the stop comes while the clip is probed
        return MagicMock(to_dict=lambda: {"duration_sec": 1.0})

    monkeypatch.setattr("src.processing.video.probe.probe_video", probe_video)
    assert _run(acct, "probe", "ast_1") == NOT_TRIED
    acct.client.put.assert_not_called()
    acct.failures.add.assert_not_called()


@pytest.mark.parametrize("status, outcome", [(422, "charged"), (503, "waits"), (500, "crash")])
def test_a_save_error_is_judged_like_every_producers(acct, monkeypatch, tmp_path, status, outcome) -> None:
    """One rule for errors saving (runner.whose): the clip's (the API refused
    it) is charged; the API or its database away waits; anything else is a
    crash, counted by the service (Crashed)."""
    (tmp_path / "ast_1.jpg").write_bytes(b"x")
    monkeypatch.setattr("src.processing.video.probe.probe_video", lambda source: MagicMock(to_dict=lambda: {}))
    acct.client.put.side_effect = LumiverbAPIError("e", "saving failed", status)
    if outcome == "crash":
        with pytest.raises(Crashed) as crashed:
            _run(acct, "probe", "ast_1")
        assert crashed.value.asset_ids == ["ast_1"]
    else:
        assert _run(acct, "probe", "ast_1") == (None if outcome == "charged" else ["ast_1"])
    assert _charged(acct) == ([("probe", "ast_1")] if outcome == "charged" else [])


def test_whose_an_error_is() -> None:
    import httpx

    assert runner.whose(LumiverbAPIError("bad", "x", 400)) == "clip"
    assert runner.whose(LumiverbAPIError("too_big", "x", 413)) == "clip"
    assert runner.whose(LumiverbAPIError("unavailable", "x", 503)) == "transient"
    assert runner.whose(LumiverbAPIError("not_found", "x", 404)) == "crash"  # one that keeps coming is counted
    assert runner.whose(httpx.ConnectError("refused")) == "transient"
    assert runner.whose(LumiverbAPIError("internal", "x", 500)) == "crash"
    assert runner.whose(RuntimeError("boom")) == "crash"


def test_this_machines_trouble_saving_isnt_the_clips(acct, monkeypatch, tmp_path) -> None:
    (tmp_path / "ast_1.jpg").write_bytes(b"x")

    def render(source, dest, settings, *, timeout):
        dest.write_bytes(b"mp4")

    monkeypatch.setattr("src.processing.video.analysis_proxy.render_analysis_proxy", render)
    acct.analysis_cache.put.side_effect = OSError(28, "No space left on device")
    with pytest.raises(Crashed):  # counted, uncharged
        _run(acct, "render", "ast_1")
    acct.failures.add.assert_not_called()


def test_work_saves_through_a_client_that_refuses_once_stopping_and_sends_no_nul(acct) -> None:
    client = runner.Saving(acct.client, acct.stopping)
    client.post("/v1/assets/ast_1/x", json={"d": "a\x00b"})
    assert acct.client.post.call_args.kwargs["json"] == {"d": "ab"}
    acct.stopping.set()
    with pytest.raises(runner.Stopped):
        client.post("/v1/assets/ast_1/x")
    assert acct.client.post.call_count == 1


def test_clips_the_api_refuses_together_are_saved_one_by_one(acct) -> None:
    """A batch the API refuses (one bad clip) is saved one clip at a time:
    only the clip it refuses is charged."""

    class Two(runner.Work):
        artifact = "faces"
        together = True

        def make(self, clip):
            return clip["asset_id"]

        def save(self, client, made):
            ids = [m for _, m in made]
            if "ast_bad" in ids:
                raise LumiverbAPIError("bad", "no", 422)
            client.post("/saved", json=ids)

    assert run(Two, acct, _job("faces", "ast_1", "ast_bad", "ast_3")) is None
    assert [c.kwargs["json"] for c in acct.client.post.call_args_list] == [["ast_1"], ["ast_3"]]
    assert _charged(acct) == [("faces", "ast_bad")]


def test_two_clips_crashing_in_a_row_leave_the_rest_waiting(acct) -> None:
    """A hung GPU kills every process: trying each of 25 clips alone would hold
    the GPU for hours."""

    class Dies(runner.Work):
        artifact = "faces"

        def make(self, clip):
            raise runner.Died("device hung")

        def save(self, client, made):
            raise AssertionError("nothing made")

    with pytest.raises(Crashed) as crashed:
        run(Dies, acct, _job("faces", "ast_1", "ast_2", "ast_3", "ast_4"))
    assert crashed.value.asset_ids == ["ast_1", "ast_2"]
    assert crashed.value.waiting == ["ast_3", "ast_4"]


# ---------------------------------------------------------------------------
# Scenes and their descriptions
# ---------------------------------------------------------------------------


def _scenes_with(acct, monkeypatch, index, tmp_path) -> object:
    monkeypatch.setattr("src.producers.scenes.work.index_video_scenes", index)
    proxy = tmp_path / "ast_1.mp4"
    proxy.write_bytes(b"x")
    acct.analysis_cache.get.return_value = proxy
    return _run(acct, "scenes", "ast_1", duration_sec=30.0)


def test_scenes_are_found_from_the_analysis_copy_and_saved_as_found(acct, monkeypatch, tmp_path) -> None:
    seen = {}

    def index(**kw):
        seen.update(kw)
        return {"scenes": 3, "chunks": 1, "elapsed": 0.1}

    assert _scenes_with(acct, monkeypatch, index, tmp_path) is None
    assert seen["asset_id"] == "ast_1" and seen["duration_sec"] == 30.0
    assert seen["lineage"]["producer"] == "scene-detect" and seen["stopping"] is acct.stopping
    assert isinstance(seen["client"], runner.Saving)  # its chunks are saved through the runner's client


def test_a_failed_chunk_charges_the_clips_scenes(acct, monkeypatch, tmp_path) -> None:
    from src.producers.scenes.work import ChunkFailed

    def index(**kw):
        raise ChunkFailed("chunk 3 failed: FFmpeg hung")

    assert _scenes_with(acct, monkeypatch, index, tmp_path) is None
    assert _charged(acct) == [("scenes", "ast_1")]


def test_scenes_that_cant_be_saved_for_now_charge_nothing(acct, monkeypatch, tmp_path) -> None:
    def index(**kw):
        raise LumiverbAPIError("database_unavailable", "away", 503)

    assert _scenes_with(acct, monkeypatch, index, tmp_path) == ["ast_1"]
    acct.failures.add.assert_not_called()


def test_scenes_stopped_mid_clip_are_not_tried(acct, monkeypatch, tmp_path) -> None:
    def index(**kw):
        acct.stopping.set()
        raise runner.Stopped(kw["asset_id"])

    assert _scenes_with(acct, monkeypatch, index, tmp_path) == NOT_TRIED
    acct.failures.add.assert_not_called()


def test_a_video_without_its_analysis_copy_or_duration_waits(acct) -> None:
    acct.analysis_cache.get.return_value = None
    assert _run(acct, "scenes", "ast_1", duration_sec=30.0) == ["ast_1"]
    assert _run(acct, "scenes", "ast_2") == ["ast_2"]
    acct.failures.add.assert_not_called()


def _described(acct, monkeypatch, tmp_path, scenes: list[dict], describe=None, patch=None, extract=None, **extra):
    proxy = tmp_path / "v.mp4"
    proxy.write_bytes(b"x")
    acct.analysis_cache.get.return_value = proxy

    def extracted(source, dest, timestamp):
        dest.write_bytes(b"jpg")
        return MagicMock(ok=True)

    monkeypatch.setattr("src.processing.video.clip_extractor.extract_video_frame_detailed", extract or extracted)
    provider = MagicMock()
    provider.describe.side_effect = describe or (lambda path: {"description": "a beach", "tags": ["sea"]})
    acct.vision.provider.return_value = provider
    acct.client.get.return_value.json.return_value = {"scenes": scenes}
    if patch is not None:
        acct.client.patch.side_effect = patch
    return _run(acct, "scene_vision", "ast_1", **extra)


def _scene(scene_id: str, ms: int = 1000, **extra) -> dict:
    return {"scene_id": scene_id, "rep_frame_ms": ms, "description": None, **extra}


def test_each_scene_is_saved_as_its_described(acct, monkeypatch, tmp_path) -> None:
    assert _described(acct, monkeypatch, tmp_path, [_scene("s1"), _scene("s2", 5000)]) is None
    patched = [c.args[0] for c in acct.client.patch.call_args_list]
    assert patched == ["/v1/video/scenes/s1", "/v1/video/scenes/s2"]
    body = acct.client.patch.call_args.kwargs["json"]
    assert body["description"] == "a beach" and body["lineage"]["producer"] == "scene-vision"
    assert "/v1/video/scenes/s2/sync" in _posted(acct)


def test_a_transient_api_error_saving_a_scene_charges_nothing(acct, monkeypatch, tmp_path) -> None:
    """#70's leftover: the error was turned into the video's failure, so the
    API or its database away was charged to the clip. Now it waits."""
    away = LumiverbAPIError("database_unavailable", "away", 503)
    assert _described(acct, monkeypatch, tmp_path, [_scene("s1")], patch=away) == ["ast_1"]
    acct.failures.add.assert_not_called()
    acct.vision.charges.assert_not_called()


def test_a_scene_dropped_while_its_described_is_skipped_not_failed(acct, monkeypatch, tmp_path) -> None:
    gone = LumiverbAPIError("scene_gone", "found again", 409)
    assert _described(acct, monkeypatch, tmp_path, [_scene("s1")], patch=gone) is None
    acct.failures.add.assert_not_called()


def test_a_scene_the_model_cant_describe_is_the_videos_once_the_rest_are_saved(acct, monkeypatch, tmp_path) -> None:
    from src.processing.workers.captions.base import CaptionError

    calls = []

    def describe(path):
        calls.append(path)
        if len(calls) == 1:
            raise CaptionError("not JSON", endpoint_fault=False)
        return {"description": "x", "tags": []}

    assert _described(acct, monkeypatch, tmp_path, [_scene("s1"), _scene("s2")], describe=describe) is None
    assert [c.args[0] for c in acct.client.patch.call_args_list] == ["/v1/video/scenes/s2"]
    assert _charged(acct) == [("scene_vision", "ast_1")]
    assert "1 of 2 scenes failed" in str(acct.failures.add.call_args.args[2])


def test_the_machines_trouble_stops_a_videos_scenes_uncharged(acct, monkeypatch, tmp_path) -> None:
    from src.processing.workers.captions.base import CaptionError

    acct.vision.charges.return_value = False
    fault = CaptionError("connection refused", endpoint_fault=True)
    assert _described(acct, monkeypatch, tmp_path, [_scene("s1")], describe=MagicMock(side_effect=fault)) == ["ast_1"]
    acct.failures.add.assert_not_called()


def test_scenes_described_this_way_already_are_skipped(acct, monkeypatch, tmp_path) -> None:
    lineage = acct.producers.lineage("scene_vision", SHA,
                                     used=acct.producers.with_model("scene_vision", acct.vision.model))
    done = _scene("s1", description="old", lineage={k: lineage[k] for k in ("producer", "version", "settings_hash")})
    assert _described(acct, monkeypatch, tmp_path, [done, _scene("s2")]) is None
    assert [c.args[0] for c in acct.client.patch.call_args_list] == ["/v1/video/scenes/s2"]


def _this_way(acct) -> dict:
    """A scene's record as this job would make it."""
    lineage = acct.producers.lineage("scene_vision", SHA,
                                     used=acct.producers.with_model("scene_vision", acct.vision.model))
    return {k: lineage[k] for k in ("producer", "version", "settings_hash")}


def test_a_redo_with_every_scene_described_this_way_describes_them_all_again(acct, monkeypatch, tmp_path) -> None:
    """Every scene matches but the video was handed out: its own record is
    what's stale (made before its file had a hash, say). Describing them all
    again records it; skipping them would hand it out forever."""
    done = [_scene("s1", description="a cat", lineage=_this_way(acct)),
            _scene("s2", description="a dog", lineage=_this_way(acct))]
    assert _described(acct, monkeypatch, tmp_path, done, redo=True) is None
    assert [c.args[0] for c in acct.client.patch.call_args_list] == ["/v1/video/scenes/s1", "/v1/video/scenes/s2"]


def test_without_a_redo_scenes_described_this_way_are_left(acct, monkeypatch, tmp_path) -> None:
    done = [_scene("s1", description="a cat", lineage=_this_way(acct))]
    assert _described(acct, monkeypatch, tmp_path, done) is None
    acct.client.patch.assert_not_called()


def test_a_scene_described_another_way_is_described_again() -> None:
    from src.producers.scene_vision.work import described

    now = {"producer": "scene-vision", "version": 2, "settings_hash": "abc"}
    assert described({"description": "x", "lineage": dict(now)}, now)
    for key, other in (("producer", "other"), ("version", 1), ("settings_hash", "def")):
        assert not described({"description": "x", "lineage": {**now, key: other}}, now)
    assert not described({"description": "x", "lineage": None}, now)  # said, but not how: described again
    assert not described({"description": None, "lineage": dict(now)}, now)


def test_a_scene_whose_record_isnt_said_counts_as_described() -> None:
    from src.producers.scene_vision.work import described

    now = {"producer": "scene-vision", "version": 2, "settings_hash": "abc"}
    assert described({"description": "x"}, now)  # no lineage key: the old rule, described is done
    assert described({"description": "x", "lineage": {}}, None)


def test_a_video_without_scenes_is_done(acct, monkeypatch, tmp_path) -> None:
    assert _described(acct, monkeypatch, tmp_path, []) is None
    acct.client.patch.assert_not_called()
    acct.failures.add.assert_not_called()


def _no_frame_at(ms: int, how: str):
    """An extract that gives no frame for the scene at ms: ok=False, or an empty file."""
    def extract(source, dest, timestamp):
        if round(timestamp * 1000) == ms:
            if how == "empty":
                dest.write_bytes(b"")
                return MagicMock(ok=True)
            return MagicMock(ok=False)
        dest.write_bytes(b"jpg")
        return MagicMock(ok=True)
    return extract


@pytest.mark.parametrize("case", ["not ok", "empty", "nothing said", "empty description"])
def test_a_scene_with_no_frame_or_no_description_is_the_videos_once_the_rest_are_saved(
        acct, monkeypatch, tmp_path, case) -> None:
    import tempfile

    frames: list[Path] = []
    real = tempfile.NamedTemporaryFile

    def named(*a, **kw):  # every frame file the job makes
        f = real(*a, **kw)
        frames.append(Path(f.name))
        return f

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", named)
    order: list[str] = []
    acct.failures.add.side_effect = lambda *a: order.append("charged")

    def patched(path, **kw):
        order.append(path)

    describe = None
    extract = None
    if case in ("not ok", "empty"):
        extract = _no_frame_at(1000, "empty" if case == "empty" else "not ok")
    else:
        said = None if case == "nothing said" else {"description": "  ", "tags": ["sea"]}
        describe = lambda path: said if path == frames[0] else {"description": "a beach", "tags": []}  # noqa: E731
    assert _described(acct, monkeypatch, tmp_path, [_scene("s1", 1000), _scene("s2", 5000)], describe=describe,
                      patch=patched, extract=extract) is None
    assert order == ["/v1/video/scenes/s2", "charged"]
    assert _charged(acct) == [("scene_vision", "ast_1")]
    assert "1 of 2 scenes failed" in str(acct.failures.add.call_args.args[2])
    assert len(frames) == 2 and not any(f.exists() for f in frames)


# ---------------------------------------------------------------------------
# Faces: the scheduler's face process, a job's photos saved together
# ---------------------------------------------------------------------------


def _faces(acct, monkeypatch, found) -> MagicMock:
    """The face work with its proxies in hand; found(asset_id) is what the process says."""
    monkeypatch.setattr("src.producers.faces.work.face_proxy", lambda item, root, cache: True)
    acct.cache.get.side_effect = lambda asset_id, rel_path=None: asset_id.encode()
    detector = MagicMock()
    detector.detect.side_effect = lambda image, settings: found(image.decode())
    acct.models.faces.return_value = detector
    return detector


def _face(asset_id: str) -> dict:
    return {"detection_model": "insightface", "detection_model_version": "buffalo_l", "embedding_model": "buffalo_l",
            "faces": [{"bounding_box": {"x": 0, "y": 0, "w": 0.1, "h": 0.1}, "detection_confidence": 0.9,
                       "embedding": [0.1]}]}


def test_a_jobs_faces_are_saved_together_and_the_clips_that_failed_charged(acct, monkeypatch) -> None:
    detector = _faces(acct, monkeypatch, lambda a: {"error": "bad image"} if a == "ast_2" else _face(a))
    assert _run(acct, "faces", "ast_1", "ast_2", "ast_3") is None
    body = _posted(acct)["/v1/assets/batch-faces"]
    assert [i["asset_id"] for i in body["items"]] == ["ast_1", "ast_3"]
    assert body["items"][0]["source_sha256"] == SHA and body["lineage"]["producer"] == "insightface"
    assert _charged(acct) == [("faces", "ast_2")]
    assert detector.detect.call_args.args[1] == acct.producers.settings("faces")


def test_faces_say_which_clips_wait(acct, monkeypatch) -> None:
    # A clip whose file changed since it was hashed waits for its scan; the
    # GPU running out of memory charges no clip.
    _faces(acct, monkeypatch, lambda a: {
        "ast_2": {"error": "[ONNXRuntimeError] : 6 : RUNTIME_EXCEPTION : CUDA failure 2: out of memory"},
        "ast_3": {"error": "Failed to allocate memory for requested buffer of size 1048576"},
        "ast_4": {"error": "bad image"}}.get(a) or _face(a))
    monkeypatch.setattr("src.producers.faces.work.face_proxy", lambda item, root, cache: item["asset_id"] != "ast_1")
    assert _run(acct, "faces", "ast_1", "ast_2", "ast_3", "ast_4", "ast_5") == ["ast_1", "ast_2", "ast_3"]
    assert _charged(acct) == [("faces", "ast_4")]


def test_a_face_photo_with_no_proxy_is_the_clips(acct, monkeypatch) -> None:
    detector = _faces(acct, monkeypatch, _face)
    acct.cache.get.side_effect = lambda asset_id, rel_path=None: None
    assert _run(acct, "faces", "ast_1") is None
    assert acct.failures.add.call_args.args == ("faces", "ast_1", "no proxy")
    detector.detect.assert_not_called()


def test_a_face_save_the_api_cant_take_now_charges_nothing(acct, monkeypatch) -> None:
    """#70's leftover: the face process saved and turned a save error into a
    string, charged to the clips. Now the parent saves, judged by whose()."""
    _faces(acct, monkeypatch, _face)
    acct.client.post.side_effect = LumiverbAPIError("database_unavailable", "away", 503)
    assert _run(acct, "faces", "ast_1", "ast_2") == ["ast_1", "ast_2"]
    acct.failures.add.assert_not_called()


def test_a_face_process_that_dies_counts_the_clip_and_the_rest_go_on(acct, monkeypatch) -> None:
    """Review (Oct 9): one photo that kills the process held the other 24 of
    its batch back. Each photo is its own call: only the one whose process
    died counts as crashed, and the rest are saved."""
    def found(a):
        if a == "ast_bad":
            raise runner.Died("face detection's process died or hung")
        return {"error": "no face data"} if a == "ast_3" else _face(a)

    _faces(acct, monkeypatch, found)
    with pytest.raises(Crashed) as crashed:
        _run(acct, "faces", "ast_1", "ast_bad", "ast_3", "ast_4")
    assert crashed.value.asset_ids == ["ast_bad"]
    assert [i["asset_id"] for i in _posted(acct)["/v1/assets/batch-faces"]["items"]] == ["ast_1", "ast_4"]
    assert _charged(acct) == [("faces", "ast_3")]


def test_a_face_process_let_go_of_meanwhile_isnt_tried(acct, monkeypatch) -> None:
    def found(a):
        raise runner.Stopped("face detection let go of")

    _faces(acct, monkeypatch, found)
    assert _run(acct, "faces", "ast_1") == NOT_TRIED
    acct.client.post.assert_not_called()


def test_a_face_proxy_is_made_from_the_original_while_its_hash_matches(tmp_path, monkeypatch) -> None:
    from src.producers.faces.work import face_proxy

    cache = MagicMock()
    cache.path = tmp_path / "proxies"
    cache.path.mkdir()
    cache.has.return_value = False
    (tmp_path / "a.jpg").write_bytes(b"photo")
    monkeypatch.setattr("src.processing.proxy.proxy_gen.generate_face_proxy", lambda source: b"proxy")
    monkeypatch.setattr("src.processing.workers.exif_extract.compute_sha256", lambda source: "sha-now")
    assert face_proxy({"asset_id": "ast_1", "rel_path": "a.jpg", "sha256": "sha-now"}, tmp_path, cache)
    cache.put.assert_called_once_with("ast_1", b"proxy")
    assert (cache.path / "ast_1.sha").read_text() == "sha-now"
    assert not face_proxy({"asset_id": "ast_2", "rel_path": "a.jpg", "sha256": "sha-then"}, tmp_path, cache)


# ---------------------------------------------------------------------------
# The scan pass
# ---------------------------------------------------------------------------


def test_a_scan_look_notes_which_libraries_can_be_reached(acct, monkeypatch) -> None:
    libs = [{"library_id": "lib_1", "name": "Footage"}]
    acct.client.get.return_value.json.return_value = libs
    acct.set_libraries = MagicMock()
    acct.set_reachable = MagicMock()
    acct.scan_state = MagicMock()
    seen = {}

    def scan_pass(client, *a, on_roots, **kw):
        seen["client"] = client
        on_roots({"lib_1": Path("/mnt/x")})  # before any scan
        return {"lib_1": Path("/mnt/x")}

    monkeypatch.setattr("src.server.scheduler.scans.scan_pass", scan_pass)
    runners.scan(acct, _job("scan", "scan:t1"), now=123.0)
    acct.set_libraries.assert_called_once_with(libs)
    assert acct.set_reachable.call_args_list[0].args == ({"lib_1": Path("/mnt/x")},)
    assert isinstance(seen["client"], runner.Saving)  # no NUL in what a scan saves (a file's EXIF)
