"""Vision work only while the account's endpoint offers its model.

The worker asks before vision steps and tells the server either way. When
the endpoint can't be used, vision work doesn't start or stops, and no clip
is charged a failure: it's the endpoint's problem. A clip that fails while
the endpoint is fine is charged.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.client.cli.failure_report import FailureReport
from src.client.cli.vision_guard import RECHECK_SEC, VisionGuard

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
    with asked, config:
        guard.check()
        clock["now"] = RECHECK_SEC
        guard.on_fail("vision")("ast_a", "the model returned nothing")
    failures.flush()
    [call] = [c for c in client.post.call_args_list if c.args[0] == "/v1/producers/failures"]
    assert call.kwargs["json"]["items"] == [
        {"asset_id": "ast_a", "artifact": "vision", "error": "the model returned nothing"}]
    assert not guard.down


def test_when_the_model_goes_away_mid_run_no_clip_is_charged_and_work_stops():
    client, clock, asked, config, guard, failures = _guard([None, "gone"])
    with asked, config:
        guard.check()
        clock["now"] = RECHECK_SEC
        guard.on_fail("ocr")("ast_a", "404 model not found")
        guard.on_fail("ocr")("ast_b", "404 model not found")  # down: not asked again, not charged
    failures.flush()
    assert not [c for c in client.post.call_args_list if c.args[0] == "/v1/producers/failures"]
    assert guard.down and _statuses(client)[-1]["ok"] is False


def test_the_endpoint_is_asked_again_at_most_every_half_minute():
    client, clock, asked, config, guard, failures = _guard([None, None])
    with asked as ask, config:
        guard.check()
        for i in range(5):
            clock["now"] = i  # within RECHECK_SEC of the check
            guard.on_fail("vision")(f"ast_{i}", "x")
    assert ask.call_count == 1
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
