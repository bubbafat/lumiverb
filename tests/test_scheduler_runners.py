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
        import threading

        self.stopping = threading.Event()
        self.gone: list[str] = []

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


def _item_posted(acct: FakeAccount, route: str) -> tuple[dict, dict]:
    """(the one item, the batch's lineage) a batch route was sent."""
    body = _posted(acct)[route]
    [item] = body["items"]
    return item, body["lineage"]


@pytest.mark.fast
def test_a_description_is_saved_with_its_lineage(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.ingest._backfill_one", lambda **kw: {
        "asset_id": kw["asset_id"], "model_id": kw["vision_model_id"], "model_version": "1",
        "description": "a dog on a beach", "tags": ["dog"]})
    assert runners.vision(acct, _job("vision", "ast_1")) is None
    # The batch route, one clip: search is left to the upkeep sweep, not committed per clip.
    item, lineage = _item_posted(acct, "/v1/assets/batch-vision")
    assert item["description"] == "a dog on a beach" and item["asset_id"] == "ast_1"
    assert item["source_sha256"] == SHA
    assert lineage == acct.producers.lineage("vision", None,
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
    body = _posted(acct)["/v1/assets/batch-ocr"]
    assert body["items"] == [{"asset_id": "ast_1", "ocr_text": "STOP", "source_sha256": SHA}]
    assert body["model_id"] == acct.vision.model and body["lineage"]["producer"] == "ocr"


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
    item, lineage = _item_posted(acct, "/v1/assets/batch-embeddings")
    assert item["vector"] == [0.1, 0.2] and item["source_sha256"] == SHA
    assert lineage["producer"] == "clip"


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
    assert runners.probe(acct, _job("probe", "ast_1")) == runners.NOT_TRIED
    probe.assert_not_called()
    acct.failures.add.assert_not_called()
    assert acct.gone == ["lib_1"]  # storage work there waits for the next look


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
    assert runners.transcript(acct, _job("transcript", "ast_1")) is None
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
    pool.apply_async.return_value.get.side_effect = [
        {"processed": 1, "failed": 1, "skipped": 0,
         "errors": [{"asset_id": "ast_2", "rel_path": "b.jpg", "error": "bad image"}]},
        {"processed": 1, "failed": 0, "skipped": 0, "errors": []}]
    made: list[int] = []
    face_runner = runners.FaceRunner(acct.client, acct.cfg, pool_factory=lambda: made.append(1) or pool)
    face_runner.run(acct, _job("faces", "ast_1", "ast_2"))
    face_runner.run(acct, _job("faces", "ast_3"))
    assert made == [1]  # the model's process is kept between batches
    args = pool.apply_async.call_args_list[0].args[1]
    assert [i["asset_id"] for i in args[2]] == ["ast_1", "ast_2"]
    assert args[4]["producer"] == "insightface"
    acct.failures.add.assert_called_once_with("faces", "ast_2", "bad image")


@pytest.mark.fast
def test_a_face_process_that_dies_charges_no_clip_and_is_replaced(acct: FakeAccount,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.repair._generate_proxy_for_item", lambda item, root, cache: item)
    import multiprocessing as mp

    dead, fresh = MagicMock(), MagicMock()
    # A process killed mid-batch never answers: the wait times out.
    dead.apply_async.return_value.get.side_effect = mp.TimeoutError()
    fresh.apply_async.return_value.get.return_value = {"errors": []}
    pools = iter([dead, fresh])
    now = [0.0]

    def clock() -> float:
        now[0] += 100.0  # each look at the clock, 100 s on
        return now[0]

    face_runner = runners.FaceRunner(acct.client, acct.cfg, pool_factory=lambda: next(pools), clock=clock)
    with pytest.raises(runners.Crashed) as crashed:  # uncharged: the service counts it towards a charge
        face_runner.run(acct, _job("faces", "ast_1"))
    assert crashed.value.asset_ids == ["ast_1"] and crashed.value.waiting == []
    assert face_runner.run(acct, _job("faces", "ast_2")) is None
    dead.terminate.assert_called_once()
    assert fresh.apply_async.called
    assert 8 <= dead.apply_async.return_value.get.call_count <= 10  # gave up after FACE_BATCH_TIMEOUT_SEC
    acct.failures.add.assert_not_called()


@pytest.mark.fast
def test_a_face_batch_whose_process_dies_is_tried_one_clip_at_a_time(acct: FakeAccount,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (Oct 9): one photo that kills the process held the other 24 of
    its batch back, uncharged, forever. Now the batch is tried again one clip
    at a time, and only the clip whose process dies again counts as crashed."""
    monkeypatch.setattr("src.client.cli.repair._generate_proxy_for_item", lambda item, root, cache: item)

    def pool() -> MagicMock:
        p = MagicMock()

        def apply_async(fn, args):
            items = args[2]
            pending = MagicMock()
            if any(i["asset_id"] == "ast_bad" for i in items):
                pending.get.side_effect = RuntimeError("segfault")  # the process died
            else:
                pending.get.return_value = {"errors": [{"asset_id": i["asset_id"], "error": "no face data"}
                                                       for i in items if i["asset_id"] == "ast_3"]}
            return pending

        p.apply_async.side_effect = apply_async
        return p

    made: list[MagicMock] = []
    face_runner = runners.FaceRunner(acct.client, acct.cfg, pool_factory=lambda: made.append(pool()) or made[-1])
    with pytest.raises(runners.Crashed) as crashed:
        face_runner.run(acct, _job("faces", "ast_1", "ast_bad", "ast_3", "ast_4"))
    assert crashed.value.asset_ids == ["ast_bad"]
    tried_alone = [[i["asset_id"] for i in c.args[1][2]] for p in made for c in p.apply_async.call_args_list][1:]
    assert tried_alone == [["ast_1"], ["ast_bad"], ["ast_3"], ["ast_4"]]
    acct.failures.add.assert_called_once_with("faces", "ast_3", "no face data")


@pytest.mark.fast
def test_probe_and_render_save_nothing_once_stopping(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch,
                                                     tmp_path: Path) -> None:
    """Review (Oct 9): probe and render saved after the scheduler said stop."""
    (tmp_path / "ast_1.jpg").write_bytes(b"x")

    def probe_video(source):
        acct.stopping.set()  # the stop comes while the clip is probed
        return MagicMock(to_dict=lambda: {"duration_sec": 1.0})

    monkeypatch.setattr("src.client.cli.repair.probe_video", probe_video)
    with pytest.raises(runners.Stopped):  # the service: not tried
        runners.probe(acct, _job("probe", "ast_1"))
    acct.client.put.assert_not_called()
    acct.failures.for_artifact.return_value.assert_not_called()


@pytest.mark.fast
@pytest.mark.parametrize("status, charged", [(422, True), (503, False), (500, False)])
def test_a_probe_save_error_is_judged_like_every_runners(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path, status: int, charged: bool) -> None:
    """One rule for errors saving (runners.whose): the clip's (the API refused
    it) is charged; others escape to the service, which charges nothing now."""
    from src.client.cli.client import LumiverbAPIError

    (tmp_path / "ast_1.jpg").write_bytes(b"x")
    monkeypatch.setattr("src.client.cli.repair.probe_video", lambda source: MagicMock(to_dict=lambda: {}))
    acct.client.put.side_effect = LumiverbAPIError("e", "saving failed", status)
    fail = acct.failures.for_artifact.return_value
    if charged:
        assert runners.probe(acct, _job("probe", "ast_1")) is None
        assert fail.call_args.args[0] == "ast_1"
    else:
        with pytest.raises(LumiverbAPIError):
            runners.probe(acct, _job("probe", "ast_1"))
        fail.assert_not_called()


@pytest.mark.fast
def test_whose_an_error_is() -> None:
    import httpx

    from src.client.cli.client import LumiverbAPIError

    assert runners.whose(LumiverbAPIError("bad", "x", 400)) == "clip"
    assert runners.whose(LumiverbAPIError("too_big", "x", 413)) == "clip"
    assert runners.whose(LumiverbAPIError("unavailable", "x", 503)) == "transient"
    assert runners.whose(LumiverbAPIError("not_found", "x", 404)) == "crash"  # one that keeps coming is counted
    assert runners.whose(httpx.ConnectError("refused")) == "transient"
    assert runners.whose(LumiverbAPIError("internal", "x", 500)) == "crash"
    assert runners.whose(RuntimeError("boom")) == "crash"


@pytest.mark.fast
def test_a_scan_look_notes_which_libraries_can_be_reached(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    libs = [{"library_id": "lib_1", "name": "Footage"}]
    acct.client.get.return_value.json.return_value = libs
    acct.set_libraries = MagicMock()
    acct.set_reachable = MagicMock()
    acct.scan_state = MagicMock()
    def scan_pass(*a, on_roots, **kw):
        on_roots({"lib_1": Path("/mnt/x")})  # before any scan
        return {"lib_1": Path("/mnt/x")}

    monkeypatch.setattr("src.server.scheduler.scans.scan_pass", scan_pass)
    runners.scan(acct, _job("scan", "scan:t1"), now=123.0)
    acct.set_libraries.assert_called_once_with(libs)
    assert acct.set_reachable.call_args_list[0].args == ({"lib_1": Path("/mnt/x")},)



@pytest.mark.fast
def test_once_stopping_nothing_more_is_saved(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path) -> None:
    # A stop (an update, a reboot) kills ffmpeg mid-clip; what a job made of
    # that mustn't be saved as the clip's (an empty transcript, for good).
    monkeypatch.setattr("src.client.cli.ingest._backfill_one", lambda **kw: {
        "asset_id": kw["asset_id"], "model_id": "m", "model_version": "1", "description": "x", "tags": []})
    acct.analysis_cache.get.return_value = tmp_path / "a.mp4"
    monkeypatch.setattr("src.client.cli.repair._transcribe_one", lambda *a: ("", ""))
    acct.stopping.set()
    assert runners.vision(acct, _job("vision", "ast_1")) == runners.NOT_TRIED
    assert runners.transcript(acct, _job("transcript", "ast_2")) == runners.NOT_TRIED
    assert runners.probe(acct, _job("probe", "ast_3")) == runners.NOT_TRIED
    acct.client.post.assert_not_called()


@pytest.mark.fast
def test_the_gpu_running_out_of_memory_charges_no_clip(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.repair._repair_embed_one",
                        MagicMock(side_effect=RuntimeError("CUDA out of memory. Tried to allocate 20 MiB")))
    runners.clip(acct, _job("clip", "ast_1"))
    acct.failures.add.assert_not_called()
    acct.client.post.assert_not_called()


@pytest.mark.fast
def test_a_transcript_whose_analysis_copy_cant_be_read_is_a_failure(acct: FakeAccount) -> None:
    acct.analysis_cache.get.return_value = None
    runners.transcript(acct, _job("transcript", "ast_1"))
    assert acct.failures.add.call_args.args[:2] == ("transcript", "ast_1")


# ---------------------------------------------------------------------------
# What a job says of each clip (review round 2): saved or reported, the
# database has it soon; one that waits without either is held the long while.
# ---------------------------------------------------------------------------


@pytest.mark.fast
def test_a_clip_the_gpu_had_no_memory_for_waits_uncharged(acct: FakeAccount,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    def embed(**kw):
        if kw["asset_id"] == "ast_2":
            raise RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB")
        return {"asset_id": kw["asset_id"], "model_id": "clip", "model_version": "v", "vector": [0.1]}

    monkeypatch.setattr("src.client.cli.repair._repair_embed_one", embed)
    assert runners.clip(acct, _job("clip", "ast_1", "ast_2")) == ["ast_2"]
    acct.failures.add.assert_not_called()


@pytest.mark.fast
def test_a_description_the_guard_didnt_charge_waits(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.client.cli.ingest._backfill_one", MagicMock(side_effect=RuntimeError("503")))
    acct.vision_fail.return_value = False  # the machines' trouble
    assert runners.vision(acct, _job("vision", "ast_1")) == ["ast_1"]
    acct.vision_fail.return_value = True  # the clip's: reported
    assert runners.vision(acct, _job("vision", "ast_2")) is None
    monkeypatch.setattr("src.client.cli.repair._ocr_one", lambda **kw: None)
    acct.vision_fail.return_value = False
    assert runners.ocr(acct, _job("ocr", "ast_3")) == ["ast_3"]


@pytest.mark.fast
def test_a_transcript_the_guard_didnt_charge_waits(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    from src.client.workers.transcripts.base import TranscriptError

    acct.analysis_cache.get.return_value = tmp_path / "a.mp4"
    monkeypatch.setattr("src.client.cli.repair._transcribe_one",
                        MagicMock(side_effect=TranscriptError("speaches broke off", endpoint_fault=True)))
    acct.transcript_fail.return_value = False
    assert runners.transcript(acct, _job("transcript", "ast_1")) == ["ast_1"]


@pytest.mark.fast
def test_a_file_gone_from_reachable_storage_waits_for_its_scan(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    (tmp_path / "other.mov").write_bytes(b"x")  # the storage is there; this file isn't
    monkeypatch.setattr("src.client.cli.repair._probe_one", MagicMock(return_value="missing"))
    assert runners.probe(acct, _job("probe", "ast_1")) == ["ast_1"]
    monkeypatch.setattr("src.client.cli.repair._render_one", MagicMock(return_value="missing"))
    assert runners.render(acct, _job("render", "ast_2")) == ["ast_2"]
    assert acct.gone == []


@pytest.mark.fast
def test_storage_that_went_away_since_the_last_look_isnt_tried(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    # Review round 2: an unmounted share is an empty folder; every file in it
    # read as missing, held an hour each.
    monkeypatch.setattr("src.client.cli.repair._probe_one", MagicMock(return_value="missing"))
    assert runners.probe(acct, _job("probe", "ast_1")) == runners.NOT_TRIED
    assert acct.gone == ["lib_1"]
    acct.roots = {"lib_1": tmp_path / "gone"}
    monkeypatch.setattr("src.client.cli.repair._render_one", MagicMock(return_value="missing"))
    assert runners.render(acct, _job("render", "ast_2")) == runners.NOT_TRIED


@pytest.mark.fast
def test_scenes_reported_as_failing_dont_wait(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    def index(**kw):
        kw["on_fail"]("ast_2", "no scenes found")
        return (1, 1)

    monkeypatch.setattr("src.client.cli.video_index.run_video_index", index)
    acct.failures.for_artifact.return_value = MagicMock(return_value=None)
    # Made scenes aren't seen here: those the server stops listing.
    assert runners.scenes(acct, _job("scenes", "ast_1", "ast_2", duration_sec=30.0)) == ["ast_1"]


@pytest.mark.fast
def test_faces_say_which_clips_wait(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    # A clip whose file changed since it was hashed waits for its scan; the
    # GPU running out of memory charges no clip.
    monkeypatch.setattr("src.client.cli.repair._generate_proxy_for_item",
                        lambda item, root, cache: None if item["asset_id"] == "ast_1" else item)
    pool = MagicMock()
    pool.apply_async.return_value.get.return_value = {"errors": [
        {"asset_id": "ast_2", "error": "[ONNXRuntimeError] : 6 : RUNTIME_EXCEPTION : CUDA failure 2: out of memory"},
        {"asset_id": "ast_3", "error": "Failed to allocate memory for requested buffer of size 1048576"},
        {"asset_id": "ast_4", "error": "bad image"}]}
    face_runner = runners.FaceRunner(acct.client, acct.cfg, pool_factory=lambda: pool)
    assert face_runner.run(acct, _job("faces", "ast_1", "ast_2", "ast_3", "ast_4", "ast_5")) == [
        "ast_1", "ast_2", "ast_3"]
    acct.failures.add.assert_called_once_with("faces", "ast_4", "bad image")


@pytest.mark.fast
def test_a_face_batch_ends_as_soon_as_its_process_is_let_go_of(acct: FakeAccount,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    # Review round 2: a stop, or the account let go of, left the GPU slot
    # (CLIP and faces, every account) waiting up to 15 minutes.
    import multiprocessing as mp
    import threading

    monkeypatch.setattr("src.client.cli.repair._generate_proxy_for_item", lambda item, root, cache: item)
    pool = MagicMock()
    pool.apply_async.return_value.get.side_effect = mp.TimeoutError()
    face_runner = runners.FaceRunner(acct.client, acct.cfg, pool_factory=lambda: pool, poll_sec=0.01,
                                     clock=lambda: 0.0)  # never times out by itself
    out: list = []
    t = threading.Thread(target=lambda: out.append(face_runner.run(acct, _job("faces", "ast_1"))), daemon=True)
    t.start()
    for _ in range(500):
        if pool.apply_async.called:
            break
        threading.Event().wait(0.01)
    assert not face_runner.idle
    assert face_runner.close() is True
    t.join(2)
    assert out == [runners.NOT_TRIED] and face_runner.idle
    assert face_runner.close() is False  # nothing left to let go of


@pytest.mark.fast
def test_a_face_pool_let_go_of_before_its_batch_starts_holds_nothing(acct: FakeAccount,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    # Review round 3: "Pool not running" read as a dead process, held an hour.
    monkeypatch.setattr("src.client.cli.repair._generate_proxy_for_item", lambda item, root, cache: item)
    pool = MagicMock()
    face_runner = runners.FaceRunner(acct.client, acct.cfg, pool_factory=lambda: pool)

    def let_go(*a, **kw):
        face_runner.close()
        raise ValueError("Pool not running")

    pool.apply_async.side_effect = let_go
    assert face_runner.run(acct, _job("faces", "ast_1")) == runners.NOT_TRIED


@pytest.mark.fast
def test_no_new_face_process_for_an_account_let_go_of(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    def proxy(item, root, cache):
        acct.stopping.set()  # the account is let go of while the proxies are made
        return item

    monkeypatch.setattr("src.client.cli.repair._generate_proxy_for_item", proxy)
    made: list[int] = []
    face_runner = runners.FaceRunner(acct.client, acct.cfg, pool_factory=lambda: made.append(1) or MagicMock())
    assert face_runner.run(acct, _job("faces", "ast_1")) == runners.NOT_TRIED and made == []


@pytest.mark.fast
def test_scene_descriptions_say_which_videos_wait(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    def enrich(**kw):
        kw["on_fail"]("ast_2", "no description")
        return (1, 1)

    monkeypatch.setattr("src.client.cli.video_index.run_video_enrich", enrich)
    acct.vision_fail.return_value = True
    assert runners.scene_vision(acct, _job("scene_vision", "ast_1", "ast_2")) == ["ast_1"]
    acct.vision_fail.return_value = False  # the machines' trouble: it waits too
    assert runners.scene_vision(acct, _job("scene_vision", "ast_1", "ast_2")) == ["ast_1", "ast_2"]



@pytest.mark.fast
def test_scenes_say_which_clips_they_made_so_only_the_rest_wait(acct: FakeAccount, monkeypatch) -> None:
    def index(**kw):
        kw["on_done"]("ast_1")
        kw["on_fail"]("ast_2", RuntimeError("bad"))
        return 1, 1
    monkeypatch.setattr("src.client.cli.video_index.run_video_index", index)
    job = _job("scenes", "ast_1", "ast_2", "ast_3", duration_sec=30.0)
    assert runners.scenes(acct, job) == ["ast_3"]  # made, reported, and one neither


@pytest.mark.fast
def test_scene_descriptions_say_which_clips_they_made(acct: FakeAccount, monkeypatch) -> None:
    def enrich(**kw):
        kw["on_done"]("ast_1")
        return 1, 0
    monkeypatch.setattr("src.client.cli.video_index.run_video_enrich", enrich)
    assert runners.scene_vision(acct, _job("scene_vision", "ast_1", "ast_2")) == ["ast_2"]


@pytest.mark.fast
def test_render_saves_through_a_client_that_refuses_once_stopping(acct: FakeAccount,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    render = MagicMock(return_value="ok")
    monkeypatch.setattr("src.client.cli.repair._render_one", render)
    runners.render(acct, _job("render", "ast_1"))
    client = render.call_args.args[0]
    client.post("/v1/assets/ast_1/artifacts/analysis_proxy")
    acct.stopping.set()
    with pytest.raises(runners.Stopped):
        client.post("/v1/assets/ast_1/artifacts/analysis_proxy")
    assert acct.client.post.call_count == 1


@pytest.mark.fast
def test_this_machines_trouble_saving_isnt_the_clips(acct: FakeAccount) -> None:
    fail = MagicMock()
    on_fail = runners._saving(fail)
    with pytest.raises(OSError):
        on_fail("ast_1", OSError(28, "No space left on device"))
    on_fail("ast_1", RuntimeError("ffprobe: invalid data"))
    fail.assert_called_once()


def _scenes_with(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch, index) -> object:
    monkeypatch.setattr("src.client.cli.video_index.index_video_scenes", index)
    acct.analysis_cache.get.return_value = MagicMock(is_file=lambda: True)
    return runners.scenes(acct, _job("scenes", "ast_1", duration_sec=30.0))


@pytest.mark.fast
def test_a_failed_chunk_charges_the_clips_scenes(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.client.cli.video_index import ChunkFailed

    def index(**kw):
        raise ChunkFailed("chunk 3 failed: FFmpeg hung")

    assert _scenes_with(acct, monkeypatch, index) is None
    acct.failures.for_artifact.assert_called_with("scenes")
    assert acct.failures.for_artifact.return_value.call_args.args[0] == "ast_1"


@pytest.mark.fast
def test_scenes_that_cant_be_saved_for_now_charge_nothing(acct: FakeAccount,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    from src.client.cli.client import LumiverbAPIError

    def index(**kw):
        raise LumiverbAPIError("database_unavailable", "away", 503)

    with pytest.raises(LumiverbAPIError):  # the service: waits, uncharged
        _scenes_with(acct, monkeypatch, index)
    acct.failures.for_artifact.return_value.assert_not_called()


@pytest.mark.fast
def test_scenes_stopped_mid_clip_are_not_tried(acct: FakeAccount, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.client.cli.video_index import Stopped

    def index(**kw):
        assert kw["stopping"] is acct.stopping
        acct.stopping.set()
        raise Stopped(kw["asset_id"])

    assert _scenes_with(acct, monkeypatch, index) == runners.NOT_TRIED
    acct.failures.for_artifact.return_value.assert_not_called()


@pytest.mark.fast
def test_face_processes_dying_twice_in_a_row_leave_the_rest_waiting(acct: FakeAccount,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """A hung GPU kills every process: trying each of 25 clips alone (15
    minutes each) would hold the GPU for hours."""
    monkeypatch.setattr("src.client.cli.repair._generate_proxy_for_item", lambda item, root, cache: item)

    def pool() -> MagicMock:
        p = MagicMock()
        p.apply_async.return_value.get.side_effect = RuntimeError("CUDA error: device hung")
        return p

    face_runner = runners.FaceRunner(acct.client, acct.cfg, pool_factory=pool)
    with pytest.raises(runners.Crashed) as crashed:
        face_runner.run(acct, _job("faces", "ast_1", "ast_2", "ast_3", "ast_4"))
    assert crashed.value.asset_ids == ["ast_1", "ast_2"]
    assert crashed.value.waiting == ["ast_3", "ast_4"]
