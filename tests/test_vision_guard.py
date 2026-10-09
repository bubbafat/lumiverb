"""Vision work only while a machine doing it offers the account's model.

The worker checks the account's machines before vision steps (Settings →
AI) and tells the server what each said. When none can be used, vision
work doesn't start or stops, and no clip is charged a failure: it's the
machines' problem. A clip is charged only when the model answered (not
usefully) and a check started after its failure finds a machine offering
the model.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.client.cli.failure_report import FailureReport
from src.client.cli.vision_guard import VisionGuard
from src.client.workers.captions.base import CaptionError
from src.shared.vision_endpoint import VisionEndpointError

pytestmark = pytest.mark.fast

QWEN = "qwen3-vl:8b"
BRAIN = {"machine_id": "aim_brain", "name": "Brain", "api_url": "http://brain/v1", "api_key": "sk-1", "at_once": 2}


def _client(machines=(BRAIN,), model=QWEN) -> MagicMock:
    client = MagicMock()
    client.get.return_value.json.return_value = {"job": "vision", "model": model, "machines": list(machines)}
    return client


def _offers(*answers):
    """The machines' model lists, check after check (a list, or an exception)."""
    def answer(*_a, **_k):
        a = next(it)
        if isinstance(a, BaseException):
            raise a
        return list(a)
    it = iter(answers)
    return patch("src.client.cli.ai_pool.list_models", side_effect=answer)


def _guard(client: MagicMock):
    clock = {"now": 0.0}
    failures = FailureReport(client)
    return clock, VisionGuard(client, failures, clock=lambda: clock["now"]), failures


def _charged(client: MagicMock) -> list:
    return [c for c in client.post.call_args_list if c.args[0] == "/v1/producers/failures"]


def test_a_machine_offering_the_model_goes_ahead_and_says_so():
    client = _client()
    _, guard, _ = _guard(client)
    with _offers((QWEN,)):
        assert guard.check() is True
    assert guard.model == QWEN and guard.capacity() == 2
    assert "Brain (2 at once)" in guard.describe()
    [status] = [c.kwargs["json"] for c in client.post.call_args_list if c.args[0].endswith("/status")]
    assert status == {"online": True, "error": "", "models": [QWEN]}


def test_no_machine_offering_the_model_stops_vision_and_says_why():
    client = _client()
    _, guard, _ = _guard(client)
    with _offers(("llava:13b",)):
        assert guard.check() is False
    assert guard.down and "doesn't offer qwen3-vl:8b" in guard.error and "Settings → AI" in guard.error


def test_with_no_model_chosen_nothing_is_asked_and_vision_waits():
    _, guard, _ = _guard(_client(model=""))
    with _offers() as asked:
        assert guard.check() is False
    asked.assert_not_called()
    assert "Settings → AI" in guard.error


def test_the_settings_unreadable_means_no_vision_this_time():
    client = MagicMock()
    client.get.side_effect = RuntimeError("502")
    guard = VisionGuard(client)
    assert guard.check() is False
    assert "Couldn't read" in guard.error


def test_a_clip_that_fails_while_a_machine_is_fine_is_charged():
    client = _client()
    clock, guard, failures = _guard(client)
    with _offers((QWEN,), (QWEN,)) as asked:
        guard.check()
        clock["now"] = 1.0
        assert guard.on_fail("vision")("ast_a", "the model returned nothing") is True  # charged
    assert asked.call_count == 2  # asked again after the failure, before charging
    failures.flush()
    [call] = _charged(client)
    assert call.kwargs["json"]["items"] == [
        {"asset_id": "ast_a", "artifact": "vision", "error": "the model returned nothing"}]
    assert not guard.down


def test_when_the_model_goes_away_mid_run_no_clip_is_charged_and_work_stops():
    client = _client()
    clock, guard, failures = _guard(client)
    with _offers((QWEN,), VisionEndpointError("gone")):
        guard.check()
        clock["now"] = 1.0
        assert guard.on_fail("ocr")("ast_a", "404 model not found") is False
        assert guard.on_fail("ocr")("ast_b", "404 model not found") is False  # down: not asked again, not charged
    failures.flush()
    assert not _charged(client) and guard.down


def test_no_machine_left_stops_vision_without_asking_or_charging():
    """The pooled provider says it's the endpoint's fault only once every
    machine has failed (out of memory, unreachable, a 5xx)."""
    client = _client()
    _, guard, failures = _guard(client)
    with _offers((QWEN,)) as asked:
        guard.check()
        assert guard.on_fail("vision")("ast_a", CaptionError(
            "No machine doing descriptions & text is online (Brain: 503)", endpoint_fault=True)) is False
        assert guard.on_fail("vision")("ast_b", "x") is False  # stopped: nothing more is charged
    failures.flush()
    assert asked.call_count == 1
    assert guard.down and "503" in guard.error
    assert not _charged(client)


@pytest.mark.parametrize(("served_by_after", "charged"), [("online", True), ("offline", False)])
def test_a_clip_isnt_charged_when_the_machine_that_served_it_turns_out_down(served_by_after, charged):
    """Two machines; Brain gave an empty answer. If Brain is found unreachable
    when checked again, that was Brain's doing, not the clip's, even with the
    Studio still fine."""
    studio = {**BRAIN, "machine_id": "aim_studio", "name": "Studio", "api_url": "http://studio/v1"}
    client = _client((BRAIN, studio))
    clock, guard, failures = _guard(client)
    brain_again = (QWEN,) if served_by_after == "online" else VisionEndpointError("no answer")
    with _offers((QWEN,), (QWEN,), brain_again, (QWEN,)):
        guard.check()
        clock["now"] = 1.0
        error = CaptionError("Empty completion content", endpoint_fault=False)
        error.machine = guard.pool.machines[0]
        assert guard.on_fail("vision")("ast_a", error) is charged
    failures.flush()
    assert bool(_charged(client)) is charged
    assert not guard.down


def test_a_clip_is_charged_only_after_a_check_that_started_after_its_failure():
    client = _client()
    clock, guard, _ = _guard(client)
    with _offers((QWEN,), (QWEN,), (QWEN,)) as asked:
        guard.check()  # at 0
        for t in (1.0, 2.0):
            clock["now"] = t
            guard.on_fail("vision")(f"ast_{t}", "unparseable answer")
    assert asked.call_count == 3


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


def _repair(tmp_path, monkeypatch, client, summary, job_type, **patches):
    from pathlib import Path

    from rich.console import Console

    from src.client.cli.config import CLIConfig, save_config
    from src.client.cli.repair import run_repair

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    save_config(CLIConfig())
    with patch("src.client.cli.repair.get_repair_summary", return_value=summary):
        run_repair(client, {"library_id": "lib_1", "name": "L", "root_path": str(tmp_path)},
                   job_type=job_type, console=Console(quiet=True))


def test_a_vision_step_doesnt_start_without_a_usable_machine(tmp_path, monkeypatch):
    client = _client()
    with (
        patch("src.client.cli.repair._page_missing") as page,
        _offers(*[VisionEndpointError("no answer")] * 5),
        patch("src.client.cli.ingest.run_backfill_vision") as backfill,
    ):
        _repair(tmp_path, monkeypatch, client,
                {"total_assets": 3, "missing_vision": 1, "missing_ocr": 1, "missing_scene_vision": 1}, "all")
    backfill.assert_not_called()
    assert not [c for c in page.call_args_list if c.kwargs.get("missing_ocr") or c.kwargs.get("missing_scene_vision")]
    assert not _charged(client)


def test_descriptions_go_to_every_online_machine_as_many_at_once_as_they_take(tmp_path, monkeypatch):
    studio = {**BRAIN, "machine_id": "aim_studio", "name": "Studio", "api_url": "http://studio/v1", "at_once": 4}
    client = _client((BRAIN, studio))
    with (
        _offers((QWEN,), (QWEN,)),
        patch("src.client.cli.ingest.run_backfill_vision") as backfill,
    ):
        _repair(tmp_path, monkeypatch, client, {"total_assets": 3, "missing_vision": 3}, "vision")
    kwargs = backfill.call_args.kwargs
    assert kwargs["concurrency"] == 6 and kwargs["model"] == QWEN
    assert kwargs["provider"] is not None


def test_ocr_stops_when_no_machine_is_left_and_charges_no_clip(tmp_path, monkeypatch):
    client = _client((dict(BRAIN, at_once=1),))
    page = [{"asset_id": f"ast_{i}", "rel_path": f"{i}.jpg"} for i in range(3)]
    with (
        patch("src.client.cli.repair._page_missing", return_value=page),
        _offers((QWEN,)),
        patch("src.client.cli.repair._ocr_one",
              side_effect=CaptionError("No machine doing descriptions & text is online (Brain: 503)",
                                       endpoint_fault=True)) as ocr,
    ):
        _repair(tmp_path, monkeypatch, client, {"total_assets": 3, "missing_ocr": 3}, "ocr")
    assert ocr.call_count == 1  # stopped after no machine was left
    assert not _charged(client)


def test_ocr_runs_as_many_at_once_as_the_machines_take(tmp_path, monkeypatch):
    import threading
    import time

    client = _client((dict(BRAIN, at_once=3),))
    page = [{"asset_id": f"ast_{i}", "rel_path": f"{i}.jpg"} for i in range(6)]
    now, most = [0], [0]
    lock = threading.Lock()

    def ocr_one(**kw):
        with lock:
            now[0] += 1
            most[0] = max(most[0], now[0])
        time.sleep(0.1)
        with lock:
            now[0] -= 1
        return {"asset_id": kw["asset_id"], "ocr_text": "EXIT"}

    with (
        patch("src.client.cli.repair._page_missing", return_value=page),
        _offers((QWEN,)),
        patch("src.client.cli.repair._ocr_one", side_effect=ocr_one),
    ):
        _repair(tmp_path, monkeypatch, client, {"total_assets": 6, "missing_ocr": 6}, "ocr")
    assert most[0] == 3
    sent = [c.kwargs["json"] for c in client.post.call_args_list if c.args[0] == "/v1/assets/batch-ocr"]
    assert sorted(i["asset_id"] for b in sent for i in b["items"]) == [f"ast_{i}" for i in range(6)]


# Scene descriptions: videos ({asset_id: scenes to describe}) on the account's
# machines, each machine answering with describe(machine_url, frame).


def _scene_server(machines, videos: dict[str, int]) -> MagicMock:
    client = _client(machines)
    job = client.get.return_value.json.return_value

    def get(path, **_kwargs):
        resp = MagicMock()
        if path.startswith("/v1/video/"):
            asset_id = path.split("/")[3]
            resp.json.return_value = {"scenes": [
                {"scene_id": f"{asset_id}_s{i}", "rep_frame_ms": i * 1000, "description": None}
                for i in range(videos[asset_id])]}
        else:
            resp.json.return_value = job
        return resp

    client.get.side_effect = get
    return client


def _describe_scenes(tmp_path, monkeypatch, client, videos: dict[str, int], describe) -> None:
    page = [{"asset_id": a, "rel_path": f"{a}.mov", "sha256": f"sha_{a}", "has_analysis_proxy": True}
            for a in videos]

    def proxy(asset_id):
        path = tmp_path / f"{asset_id}.mp4"
        path.write_bytes(b"proxy")
        return path

    def frame(source, dest, timestamp):
        dest.write_text(f"{source.stem}_s{int(timestamp)}")  # the scene it's of
        return MagicMock(ok=True)

    def provider(model, url, key, settings=None, ocr_settings=None):
        p = MagicMock()
        p.describe.side_effect = lambda path: describe(url, path.read_text())
        return p

    with (
        patch("src.client.cli.repair._page_missing", return_value=page),
        patch("src.client.proxy.analysis_cache.AnalysisProxyCache.get", side_effect=proxy),
        patch("src.client.cli.video_index.extract_video_frame_detailed", side_effect=frame),
        patch("src.client.workers.captions.factory.get_caption_provider", side_effect=provider),
        patch("src.client.cli.ai_pool.list_models", return_value=[QWEN]),
    ):
        _repair(tmp_path, monkeypatch, client,
                {"total_assets": len(videos), "missing_scene_vision": len(videos)}, "scene-vision")


def _described(client) -> dict[str, dict]:
    """Scene id → what was recorded for it."""
    return {c.args[0].rsplit("/", 1)[1]: c.kwargs["json"] for c in client.patch.call_args_list
            if c.args[0].startswith("/v1/video/scenes/")}


def _listed(client) -> list[str]:
    """The videos whose scenes were asked for, in order."""
    return [c.args[0].split("/")[3] for c in client.get.call_args_list if c.args[0].startswith("/v1/video/")]


STUDIO = {**BRAIN, "machine_id": "aim_studio", "name": "Studio", "api_url": "http://studio/v1", "at_once": 4}


def test_scene_descriptions_run_as_many_at_once_as_the_machines_take(tmp_path, monkeypatch):
    import threading
    import time

    # Short clips: no one video has enough scenes to keep the machines busy.
    videos = {f"ast_{v}": 3 for v in range(4)}
    client = _scene_server((BRAIN, STUDIO), videos)
    now: dict[str, int] = {}
    most: dict[str, int] = {}
    lock = threading.Lock()

    def describe(url, _frame):
        with lock:
            now[url] = now.get(url, 0) + 1
            now["all"] = now.get("all", 0) + 1
            for k in (url, "all"):
                most[k] = max(most.get(k, 0), now[k])
        time.sleep(0.15)
        with lock:
            now[url] -= 1
            now["all"] -= 1
        return {"description": "a street at night", "tags": ["street"]}

    _describe_scenes(tmp_path, monkeypatch, client, videos, describe)
    assert most["all"] == 6
    assert most["http://brain/v1"] <= 2 and most["http://studio/v1"] <= 4
    described = _described(client)
    assert sorted(described) == sorted(f"{a}_s{i}" for a in videos for i in range(3))
    # Each scene's lineage is its own video's.
    assert all(d["lineage"]["source_sha256"] == f"sha_{sid.rsplit('_s', 1)[0]}" for sid, d in described.items())
    assert not _charged(client)


def test_a_scene_goes_to_another_machine_when_one_fails(tmp_path, monkeypatch):
    videos = {f"ast_{v}": 3 for v in range(3)}
    client = _scene_server((BRAIN, STUDIO), videos)
    served: list[str] = []

    def describe(url, _frame):
        served.append(url)
        if url == "http://brain/v1":
            raise CaptionError("500 out of memory", endpoint_fault=True)
        return {"description": "a beach", "tags": ["beach"]}

    _describe_scenes(tmp_path, monkeypatch, client, videos, describe)
    assert sorted(_described(client)) == sorted(f"{a}_s{i}" for a in videos for i in range(3))
    assert served.count("http://brain/v1") <= 2  # skipped once it failed
    brain = [c.kwargs["json"] for c in client.post.call_args_list if c.args[0] == "/v1/ai/machines/aim_brain/status"]
    assert brain[-1]["online"] is False
    assert not _charged(client)


def test_scene_descriptions_stop_when_no_machine_is_left_and_charge_no_clip(tmp_path, monkeypatch):
    videos = {f"ast_{v}": 2 for v in range(5)}
    client = _scene_server((BRAIN,), videos)
    served: list[str] = []

    def describe(url, _frame):
        served.append(url)
        raise CaptionError("connection refused", endpoint_fault=True)

    _describe_scenes(tmp_path, monkeypatch, client, videos, describe)
    assert _listed(client) == ["ast_0"]  # the rest wait for a machine
    assert len(served) <= 2
    assert not _described(client)
    assert not _charged(client)


def test_a_scene_that_cant_be_described_is_reported_for_its_video(tmp_path, monkeypatch):
    videos = {"ast_a": 3, "ast_b": 2}
    client = _scene_server((BRAIN, STUDIO), videos)

    def describe(_url, scene):
        if scene == "ast_a_s1":
            raise CaptionError("unparseable answer", endpoint_fault=False)
        return {"description": "a dog", "tags": ["dog"]}

    _describe_scenes(tmp_path, monkeypatch, client, videos, describe)
    charged = [i for c in _charged(client) for i in c.kwargs["json"]["items"]]
    assert [(i["asset_id"], i["artifact"], i["error"]) for i in charged] == [
        ("ast_a", "scene_vision", "1 of 3 scenes failed: unparseable answer")]
    assert sorted(_described(client)) == ["ast_a_s0", "ast_a_s2", "ast_b_s0", "ast_b_s1"]


def test_each_machine_is_asked_for_the_model_by_its_own_name():
    client = _client()
    guard = VisionGuard(client)
    with _offers((QWEN,)):
        assert guard.check()
    guard.pool.machines[0].serves = "qwen3-vl:8b-q4"  # what that machine lists it as
    with patch("src.client.workers.captions.factory.get_caption_provider") as make:
        make.return_value.describe.return_value = {"description": "a cat", "tags": []}
        guard.provider().describe("a.jpg")
    assert make.call_args.args[0] == "qwen3-vl:8b-q4"
