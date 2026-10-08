"""Vision work only while the account's endpoint offers its model.

The worker asks before vision steps and tells the server either way. When
the endpoint can't be used, vision work doesn't start or stops, and no clip
is charged a failure: it's the endpoint's problem. A clip is charged only
when the model answered (not usefully) and a check started after its
failure finds the endpoint offering the model.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.client.cli.failure_report import FailureReport
from src.client.cli.vision_guard import VisionGuard
from src.client.workers.captions.base import CaptionError

pytestmark = pytest.mark.fast

URL = "http://vision.local:11434/v1"
SETTINGS = (URL, "sk-1", "qwen3-vl:8b", "account settings")


def _guard(answers: list[str | None], settings=SETTINGS):
    """A guard whose endpoint answers each check in turn (None: it offers the model)."""
    client = MagicMock()
    clock = {"now": 0.0}
    asked = patch("src.client.cli.vision_guard.check_model", side_effect=answers)
    config = patch("src.client.cli.ingest._resolve_vision_config", return_value=settings)
    failures = FailureReport(client)
    return client, clock, asked, config, VisionGuard(client, failures, clock=lambda: clock["now"]), failures


def _statuses(client: MagicMock) -> list[dict]:
    return [c.kwargs["json"] for c in client.post.call_args_list if c.args[0] == "/v1/tenant/vision/status"]


def test_a_model_the_endpoint_offers_goes_ahead_and_says_so():
    client, _, asked, config, guard, _ = _guard([None])
    with asked as ask, config:
        assert guard.check() is True
    ask.assert_called_once_with(URL, "sk-1", "qwen3-vl:8b")
    assert (guard.api_url, guard.api_key, guard.model) == (URL, "sk-1", "qwen3-vl:8b")
    assert _statuses(client) == [{"ok": True, "error": "", "model": "qwen3-vl:8b", "api_url": URL}]


def test_a_model_the_endpoint_no_longer_offers_stops_vision_and_says_why():
    client, _, asked, config, guard, _ = _guard([f"{URL} no longer offers qwen3-vl:8b."])
    with asked, config:
        assert guard.check() is False
    assert guard.down
    [status] = _statuses(client)
    assert status["ok"] is False and "no longer offers" in status["error"]


def test_with_no_model_chosen_nothing_is_asked_and_vision_waits():
    client, _, asked, config, guard, _ = _guard([], settings=(URL, None, "", "account settings"))
    with asked as ask, config:
        assert guard.check() is False
    ask.assert_not_called()
    assert "Settings → AI" in guard.error
    assert _statuses(client)[0]["ok"] is False  # shown in Settings


def test_vision_off_shows_nothing():
    client, _, asked, config, guard, _ = _guard([], settings=("", None, "", "account settings"))
    with asked, config:
        assert guard.check() is False
    assert _statuses(client) == []


def test_a_clip_that_fails_while_the_endpoint_is_fine_is_charged():
    client, clock, asked, config, guard, failures = _guard([None, None])
    with asked as ask, config:
        guard.check()
        clock["now"] = 1.0
        guard.on_fail("vision")("ast_a", "the model returned nothing")
    assert ask.call_count == 2  # asked again after the failure, before charging
    failures.flush()
    [call] = [c for c in client.post.call_args_list if c.args[0] == "/v1/producers/failures"]
    assert call.kwargs["json"]["items"] == [
        {"asset_id": "ast_a", "artifact": "vision", "error": "the model returned nothing"}]
    assert not guard.down


def test_when_the_model_goes_away_mid_run_no_clip_is_charged_and_work_stops():
    client, clock, asked, config, guard, failures = _guard([None, "gone"])
    with asked, config:
        guard.check()
        clock["now"] = 1.0
        guard.on_fail("ocr")("ast_a", "404 model not found")
        guard.on_fail("ocr")("ast_b", "404 model not found")  # down: not asked again, not charged
    failures.flush()
    assert not [c for c in client.post.call_args_list if c.args[0] == "/v1/producers/failures"]
    assert guard.down and _statuses(client)[-1]["ok"] is False


def test_the_endpoints_own_fault_stops_vision_without_asking_or_charging():
    """Listed but unable to serve (out of memory, overloaded, a 5xx): the
    model list says fine, the answer says otherwise."""
    client, clock, asked, config, guard, failures = _guard([None])
    with asked as ask, config:
        guard.check()
        guard.on_fail("vision")("ast_a", CaptionError("503 Service Unavailable", endpoint_fault=True))
        guard.on_fail("vision")("ast_b", "x")  # stopped: nothing more is charged
    failures.flush()
    assert ask.call_count == 1
    assert guard.down and "503" in guard.error
    assert not [c for c in client.post.call_args_list if c.args[0] == "/v1/producers/failures"]
    assert _statuses(client)[-1]["ok"] is False


def test_a_clip_is_charged_only_after_a_check_that_started_after_its_failure():
    client, clock, asked, config, guard, failures = _guard([None, None, None])
    with asked as ask, config:
        guard.check()  # at 0
        for t in (1.0, 2.0):
            clock["now"] = t
            guard.on_fail("vision")(f"ast_{t}", "unparseable answer")
    assert ask.call_count == 3
    failures.flush()


def test_the_settings_unreadable_means_no_vision_this_time():
    client = MagicMock()
    with patch("src.client.cli.ingest._resolve_vision_config", side_effect=RuntimeError("502")):
        guard = VisionGuard(client)
        assert guard.check() is False
    assert "Couldn't read" in guard.error


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------


def test_a_vision_step_doesnt_start_without_a_usable_model(tmp_path, monkeypatch):
    from pathlib import Path

    from rich.console import Console

    from src.client.cli.config import CLIConfig, save_config
    from src.client.cli.repair import run_repair

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    save_config(CLIConfig())
    client = MagicMock()
    with (
        patch("src.client.cli.repair.get_repair_summary",
              return_value={"total_assets": 3, "missing_vision": 1, "missing_ocr": 1, "missing_scene_vision": 1}),
        patch("src.client.cli.repair._page_missing") as page,
        patch("src.client.cli.ingest._resolve_vision_config", return_value=SETTINGS),
        patch("src.client.cli.vision_guard.check_model", return_value="gone"),
        patch("src.client.cli.ingest.run_backfill_vision") as backfill,
    ):
        run_repair(client, {"library_id": "lib_1", "name": "L", "root_path": str(tmp_path)},
                   job_type="all", console=Console(quiet=True))
    backfill.assert_not_called()
    assert not [c for c in page.call_args_list if c.kwargs.get("missing_ocr") or c.kwargs.get("missing_scene_vision")]
    assert not [c for c in client.post.call_args_list if c.args[0] == "/v1/producers/failures"]


def test_ocr_stops_when_the_endpoint_fails_and_charges_no_clip(tmp_path, monkeypatch):
    from pathlib import Path

    from rich.console import Console

    from src.client.cli.config import CLIConfig, save_config
    from src.client.cli.repair import run_repair

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    save_config(CLIConfig())
    client = MagicMock()
    page = [{"asset_id": f"ast_{i}", "rel_path": f"{i}.jpg"} for i in range(3)]
    with (
        patch("src.client.cli.repair.get_repair_summary", return_value={"total_assets": 3, "missing_ocr": 3}),
        patch("src.client.cli.repair._page_missing", return_value=page),
        patch("src.client.cli.ingest._resolve_vision_config", return_value=SETTINGS),
        patch("src.client.cli.vision_guard.check_model", return_value=None),
        patch("src.client.workers.captions.factory.get_caption_provider"),
        patch("src.client.cli.repair._ocr_one",
              side_effect=CaptionError("503 Service Unavailable", endpoint_fault=True)) as ocr,
    ):
        run_repair(client, {"library_id": "lib_1", "name": "L", "root_path": str(tmp_path)},
                   job_type="ocr", console=Console(quiet=True))
    assert ocr.call_count == 1  # stopped after the endpoint failed
    assert not [c for c in client.post.call_args_list if c.args[0] == "/v1/producers/failures"]
    assert _statuses(client)[-1]["ok"] is False


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

    with patch("src.client.cli.video_index.extract_video_frame_detailed", side_effect=frame):
        with pytest.raises(CaptionError):
            enrich_video_scenes(client=client, source_path=tmp_path / "a.mp4", asset_id="ast_v", rel_path="v.mov",
                                vision_provider=provider, vision_model_id="m")
    assert provider.describe.call_count == 1
