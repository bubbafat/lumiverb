"""Running one job of each kind (ADR-016 phase 4).

Each runner does what the worker's step did for a clip, with the same
per-clip code, and saves the result through the API with its lineage. A
clip that can't be made is reported as failing; the AI machines' trouble
goes through their guard, which charges no clip for it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.client.cli.producer_settings import ProducerSettings
from src.server.scheduler import runners
from src.server.scheduler.dispatch import Job

SHA = "a" * 64


class FakeAccount:
    def __init__(self, tmp_path: Path) -> None:
        self.tenant_id = "t1"
        self.client = MagicMock()
        self.client.base_url, self.client.token = "http://api", "lv_key"
        self.producers = ProducerSettings(None)
        self.failures = MagicMock()
        self.vision = MagicMock()
        self.vision.model = "qwen3-vl:8b-instruct"
        self.vision_fail = MagicMock()
        self.vision.on_fail.return_value = self.vision_fail
        self.transcripts = MagicMock()
        self.transcripts.model = "small"
        self.transcript_fail = MagicMock()
        self.transcripts.on_fail.return_value = self.transcript_fail
        self.analysis_cache = MagicMock()
        self.cfg = MagicMock(analysis_proxy_encoder="libx264", analysis_proxy_decoder="cpu", gpu_decodes=0,
                             face_batch_limit=20)
        self.roots = {"lib_1": tmp_path}
        self.cache = MagicMock()
        self.cache.path = tmp_path / "proxies"

    def root(self, library_id: str):
        return self.roots.get(library_id)

    def proxy_cache(self, library_id: str):
        return self.cache

    def vision_provider(self):
        return "provider", self.vision.model

    def clip(self):
        provider = MagicMock()
        return provider, {**self.producers.settings("clip"), "input_edge": 1280}

    def transcriber(self):
        return "transcriber"


def _job(kind: str, *ids: str, **extra) -> Job:
    return Job("t1", kind, 3, tuple({"asset_id": i, "library_id": "lib_1", "rel_path": f"{i}.jpg", "sha256": SHA,
                                     **extra} for i in ids))


@pytest.fixture
def acct(tmp_path: Path) -> FakeAccount:
    return FakeAccount(tmp_path)


def _posted(acct: FakeAccount) -> dict[str, dict]:
    return {c.args[0]: c.kwargs["json"] for c in acct.client.post.call_args_list}


@pytest.mark.fast
def test_a_description_is_saved_with_its_lineage(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.ingest._backfill_one", lambda **kw: {
        "asset_id": kw["asset_id"], "model_id": kw["vision_model_id"], "model_version": "1",
        "description": "a dog on a beach", "tags": ["dog"]})
    runners.vision(acct, _job("vision", "ast_1"))
    body = _posted(acct)["/v1/assets/ast_1/vision"]
    assert body["description"] == "a dog on a beach"
    assert body["lineage"] == acct.producers.lineage("vision", SHA,
                                                     used=acct.producers.with_model("vision", acct.vision.model))


@pytest.mark.fast
def test_a_description_that_fails_goes_through_the_machines_guard(acct: FakeAccount,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    error = RuntimeError("machine gone")
    monkeypatch.setattr("src.client.cli.ingest._backfill_one", MagicMock(side_effect=error))
    runners.vision(acct, _job("vision", "ast_1"))
    acct.vision_fail.assert_called_once_with("ast_1", error)
    acct.client.post.assert_not_called()


@pytest.mark.fast
def test_text_in_an_image_is_saved_with_its_lineage(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.repair._ocr_one", lambda **kw: {"asset_id": kw["asset_id"],
                                                                        "ocr_text": "STOP"})
    runners.ocr(acct, _job("ocr", "ast_1"))
    body = _posted(acct)["/v1/assets/ast_1/ocr"]
    assert body["ocr_text"] == "STOP" and body["model_id"] == acct.vision.model
    assert body["lineage"]["producer"] == "ocr" and body["lineage"]["source_sha256"] == SHA


@pytest.mark.fast
def test_no_text_result_is_reported_through_the_guard(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.repair._ocr_one", lambda **kw: None)
    runners.ocr(acct, _job("ocr", "ast_1"))
    assert acct.vision_fail.call_args.args[0] == "ast_1"


@pytest.mark.fast
def test_a_clip_embedding_is_saved_with_its_lineage(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.repair._repair_embed_one", lambda **kw: {
        "asset_id": kw["asset_id"], "model_id": "clip", "model_version": "ViT-B-32", "vector": [0.1, 0.2]})
    runners.clip(acct, _job("clip", "ast_1"))
    body = _posted(acct)["/v1/assets/ast_1/embeddings"]
    assert body["vector"] == [0.1, 0.2] and body["source_sha256"] == SHA
    assert body["lineage"]["producer"] == "clip"


@pytest.mark.fast
def test_an_embedding_that_cannot_be_made_is_a_failure(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.repair._repair_embed_one", lambda **kw: None)
    runners.clip(acct, _job("clip", "ast_1"))
    assert acct.failures.add.call_args.args[:2] == ("clip", "ast_1")


@pytest.mark.fast
def test_probing_waits_while_the_storage_is_away(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = MagicMock()
    monkeypatch.setattr("src.client.cli.repair._probe_one", probe)
    acct.roots = {}
    runners.probe(acct, _job("probe", "ast_1"))
    probe.assert_not_called()
    acct.failures.add.assert_not_called()


@pytest.mark.fast
def test_a_probe_runs_on_the_librarys_storage(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    probe = MagicMock(return_value="ok")
    monkeypatch.setattr("src.client.cli.repair._probe_one", probe)
    runners.probe(acct, _job("probe", "ast_1"))
    assert probe.call_args.args[1] == tmp_path
    assert probe.call_args.args[2]["asset_id"] == "ast_1"


@pytest.mark.fast
def test_rendering_uses_the_servers_settings_and_this_machines_decoder(acct: FakeAccount,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    render = MagicMock(return_value="ok")
    monkeypatch.setattr("src.client.cli.repair._render_one", render)
    runners.render(acct, _job("render", "ast_1"))
    settings = render.call_args.args[3]
    assert settings.decoder == "cpu" and settings.gpu_decodes == 0
    assert settings.max_edge == acct.producers.settings("analysis_proxy")["max_edge"]


@pytest.mark.fast
def test_a_transcript_is_saved_with_its_lineage(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    acct.analysis_cache.get.return_value = tmp_path / "a.mp4"
    heard = MagicMock(return_value=("1\n00:00:00,000 --> 00:00:01,000\nhola\n", "es"))
    monkeypatch.setattr("src.client.cli.repair._transcribe_one", heard)
    runners.transcript(acct, _job("transcript", "ast_1"))
    assert heard.call_args.args == (tmp_path / "a.mp4", "transcriber", 500)
    body = _posted(acct)["/v1/assets/ast_1/transcript"]
    assert body["language"] == "es" and body["source"] == "whisper"
    assert body["lineage"]["producer"] == "whisper"


@pytest.mark.fast
def test_the_machines_trouble_with_a_transcript_charges_no_clip_here(acct: FakeAccount,
                                                                     monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    from src.client.workers.transcripts.base import TranscriptError

    acct.analysis_cache.get.return_value = tmp_path / "a.mp4"
    error = TranscriptError("speaches broke off", endpoint_fault=True)
    monkeypatch.setattr("src.client.cli.repair._transcribe_one", MagicMock(side_effect=error))
    runners.transcript(acct, _job("transcript", "ast_1"))
    acct.transcript_fail.assert_called_once_with("ast_1", error)
    acct.failures.add.assert_not_called()


@pytest.mark.fast
def test_a_transcript_that_could_not_be_tried_is_a_failure(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch,
                                                           tmp_path: Path) -> None:
    acct.analysis_cache.get.return_value = tmp_path / "a.mp4"
    monkeypatch.setattr("src.client.cli.repair._transcribe_one", lambda *a: None)
    runners.transcript(acct, _job("transcript", "ast_1"))
    assert acct.failures.add.call_args.args[:2] == ("transcript", "ast_1")


@pytest.mark.fast
def test_scenes_are_found_from_the_analysis_copy(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    index = MagicMock(return_value=(1, 0))
    monkeypatch.setattr("src.client.cli.video_index.run_video_index", index)
    runners.scenes(acct, _job("scenes", "ast_1", duration_sec=30.0))
    [video] = index.call_args.kwargs["videos"]
    assert video["asset_id"] == "ast_1" and video["duration_sec"] == 30.0
    assert index.call_args.kwargs["lineage_for"](video)["producer"] == "scene-detect"


@pytest.mark.fast
def test_scene_descriptions_go_one_at_a_time_per_job(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    enrich = MagicMock(return_value=(1, 0))
    monkeypatch.setattr("src.client.cli.video_index.run_video_enrich", enrich)
    runners.scene_vision(acct, _job("scene_vision", "ast_1"))
    assert enrich.call_args.kwargs["concurrency"] == 1
    assert enrich.call_args.kwargs["vision_model_id"] == acct.vision.model


@pytest.mark.fast
def test_faces_run_in_a_kept_subprocess_and_report_the_clips_that_failed(acct: FakeAccount,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.repair._generate_proxy_for_item", lambda item, root, cache: item)
    pool = MagicMock()
    pool.apply.side_effect = [{"processed": 1, "failed": 1, "skipped": 0,
                               "errors": [{"asset_id": "ast_2", "rel_path": "b.jpg", "error": "bad image"}]},
                              {"processed": 1, "failed": 0, "skipped": 0, "errors": []}]
    made: list[int] = []
    face_runner = runners.FaceRunner(acct.client, acct.cfg, pool_factory=lambda: made.append(1) or pool)
    face_runner.run(acct, _job("faces", "ast_1", "ast_2"))
    face_runner.run(acct, _job("faces", "ast_3"))
    assert made == [1]  # the model's process is kept between batches
    args = pool.apply.call_args_list[0].args[1]
    assert [i["asset_id"] for i in args[2]] == ["ast_1", "ast_2"]
    assert args[4]["producer"] == "insightface"
    acct.failures.add.assert_called_once_with("faces", "ast_2", "bad image")


@pytest.mark.fast
def test_a_face_process_that_dies_charges_no_clip_and_is_replaced(acct: FakeAccount,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.repair._generate_proxy_for_item", lambda item, root, cache: item)
    dead, fresh = MagicMock(), MagicMock()
    dead.apply.side_effect = RuntimeError("CUDA out of memory")
    fresh.apply.return_value = {"errors": []}
    pools = iter([dead, fresh])
    face_runner = runners.FaceRunner(acct.client, acct.cfg, pool_factory=lambda: next(pools))
    face_runner.run(acct, _job("faces", "ast_1"))
    face_runner.run(acct, _job("faces", "ast_2"))
    dead.terminate.assert_called_once()
    assert fresh.apply.called
    acct.failures.add.assert_not_called()


@pytest.mark.fast
def test_a_scan_look_notes_which_libraries_can_be_reached(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    libs = [{"library_id": "lib_1", "name": "Footage"}]
    acct.client.get.return_value.json.return_value = libs
    acct.set_libraries = MagicMock()
    acct.scan_state = MagicMock()
    monkeypatch.setattr("src.server.scheduler.scans.scan_pass", lambda *a, **kw: {"lib_1": True})
    runners.scan(acct, _job("scan", "scan:t1"), now=123.0)
    acct.set_libraries.assert_called_once_with(libs)
    assert acct.reachable == {"lib_1": True}
