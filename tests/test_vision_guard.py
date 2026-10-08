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
        guard.on_fail("vision")("ast_a", "the model returned nothing")
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
        guard.on_fail("ocr")("ast_a", "404 model not found")
        guard.on_fail("ocr")("ast_b", "404 model not found")  # down: not asked again, not charged
    failures.flush()
    assert not _charged(client) and guard.down


def test_no_machine_left_stops_vision_without_asking_or_charging():
    """The pooled provider says it's the endpoint's fault only once every
    machine has failed (out of memory, unreachable, a 5xx)."""
    client = _client()
    _, guard, failures = _guard(client)
    with _offers((QWEN,)) as asked:
        guard.check()
        guard.on_fail("vision")("ast_a", CaptionError("No machine doing descriptions & text is online (Brain: 503)",
                                                      endpoint_fault=True))
        guard.on_fail("vision")("ast_b", "x")  # stopped: nothing more is charged
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
        guard.on_fail("vision")("ast_a", error)
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


def test_a_scene_that_cant_be_described_is_reported_for_its_video():
    from src.client.cli.video_index import run_video_enrich

    fails: list[tuple[str, object]] = []
    with patch("src.client.cli.video_index.enrich_video_scenes", return_value={
        "enriched": 2, "skipped": 0, "failed": 1, "errors": ["unparseable answer"], "elapsed": 0.1}):
        ok, failed = run_video_enrich(
            client=MagicMock(), source_for=lambda v: MagicMock(is_file=lambda: True),
            videos=[{"asset_id": "ast_v", "rel_path": "v.mov"}], vision_provider=MagicMock(),
            vision_model_id="m", console=MagicMock(), progress=MagicMock(), task_id=1,
            on_fail=lambda a, e: fails.append((a, e)))
    assert (ok, failed) == (0, 1)
    assert fails == [("ast_v", "1 of 3 scenes failed: unparseable answer")]


def test_the_endpoint_failing_mid_video_stops_the_video_for_the_guard(tmp_path):
    from src.client.cli.video_index import enrich_video_scenes

    client = MagicMock()
    client.get.return_value.json.return_value = {"scenes": [
        {"scene_id": f"s{i}", "rep_frame_ms": i * 1000, "description": None} for i in range(3)]}
    provider = MagicMock()
    provider.describe.side_effect = CaptionError("connection refused", endpoint_fault=True)

    def frame(_source, dest, timestamp):
        dest.write_bytes(b"jpeg")
        return MagicMock(ok=True)

    with patch("src.client.cli.video_index.extract_video_frame_detailed", side_effect=frame), \
            pytest.raises(CaptionError):
        enrich_video_scenes(client=client, source_path=tmp_path / "a.mp4", asset_id="ast_v", rel_path="v.mov",
                            vision_provider=provider, vision_model_id="m")
    assert provider.describe.call_count == 1


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
