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

from src.processing.vision_guard import VisionGuard
from src.processing.workers.captions.base import CaptionError
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
    return patch("src.processing.ai_pool.list_models", side_effect=answer)


def _guard(client: MagicMock):
    clock = {"now": 0.0}
    return clock, VisionGuard(client, clock=lambda: clock["now"])


def test_a_machine_offering_the_model_goes_ahead_and_says_so():
    client = _client()
    _, guard = _guard(client)
    with _offers((QWEN,)):
        assert guard.check() is True
    assert guard.model == QWEN and guard.capacity() == 2
    assert "Brain (2 at once)" in guard.describe()
    [status] = [c.kwargs["json"] for c in client.post.call_args_list if c.args[0].endswith("/status")]
    assert status == {"online": True, "error": "", "models": [QWEN]}


def test_no_machine_offering_the_model_stops_vision_and_says_why():
    client = _client()
    _, guard = _guard(client)
    with _offers(("llava:13b",)):
        assert guard.check() is False
    assert guard.down and "doesn't offer qwen3-vl:8b" in guard.error and "Settings → AI" in guard.error


def test_with_no_model_chosen_nothing_is_asked_and_vision_waits():
    _, guard = _guard(_client(model=""))
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
    clock, guard = _guard(client)
    with _offers((QWEN,), (QWEN,)) as asked:
        guard.check()
        clock["now"] = 1.0
        assert guard.charges("the model returned nothing") is True  # charged
    assert asked.call_count == 2  # asked again after the failure, before charging
    assert not guard.down


def test_when_the_model_goes_away_mid_run_no_clip_is_charged_and_work_stops():
    client = _client()
    clock, guard = _guard(client)
    with _offers((QWEN,), VisionEndpointError("gone")):
        guard.check()
        clock["now"] = 1.0
        assert guard.charges("404 model not found") is False
        assert guard.charges("404 model not found") is False  # down: not asked again, not charged
    assert guard.down


def test_no_machine_left_stops_vision_without_asking_or_charging():
    """The pooled provider says it's the endpoint's fault only once every
    machine has failed (out of memory, unreachable, a 5xx)."""
    client = _client()
    _, guard = _guard(client)
    with _offers((QWEN,)) as asked:
        guard.check()
        assert guard.charges(CaptionError(
            "No machine doing descriptions & text is online (Brain: 503)", endpoint_fault=True)) is False
        assert guard.charges("x") is False  # stopped: nothing more is charged
    assert asked.call_count == 1
    assert guard.down and "503" in guard.error


@pytest.mark.parametrize(("served_by_after", "charged"), [("online", True), ("offline", False)])
def test_a_clip_isnt_charged_when_the_machine_that_served_it_turns_out_down(served_by_after, charged):
    """Two machines; Brain gave an empty answer. If Brain is found unreachable
    when checked again, that was Brain's doing, not the clip's, even with the
    Studio still fine."""
    studio = {**BRAIN, "machine_id": "aim_studio", "name": "Studio", "api_url": "http://studio/v1"}
    client = _client((BRAIN, studio))
    clock, guard = _guard(client)
    brain_again = (QWEN,) if served_by_after == "online" else VisionEndpointError("no answer")
    with _offers((QWEN,), (QWEN,), brain_again, (QWEN,)):
        guard.check()
        clock["now"] = 1.0
        error = CaptionError("Empty completion content", endpoint_fault=False)
        error.machine = guard.pool.machines[0]
        assert guard.charges(error) is charged
    assert not guard.down


def test_a_clip_is_charged_only_after_a_check_that_started_after_its_failure():
    client = _client()
    clock, guard = _guard(client)
    with _offers((QWEN,), (QWEN,), (QWEN,)) as asked:
        guard.check()  # at 0
        for t in (1.0, 2.0):
            clock["now"] = t
            guard.charges("unparseable answer")
    assert asked.call_count == 3


def test_each_machine_is_asked_for_the_model_by_its_own_name():
    client = _client()
    guard = VisionGuard(client)
    with _offers((QWEN,)):
        assert guard.check()
    guard.pool.machines[0].serves = "qwen3-vl:8b-q4"  # what that machine lists it as
    with patch("src.processing.workers.captions.factory.get_caption_provider") as make:
        make.return_value.describe.return_value = {"description": "a cat", "tags": []}
        guard.provider().describe("a.jpg")
    assert make.call_args.args[0] == "qwen3-vl:8b-q4"


def test_a_clip_caught_in_a_model_change_isnt_charged_and_vision_goes_on():
    client = _client()
    _, guard = _guard(client)
    with _offers((QWEN,)):
        guard.check()
        error = CaptionError("The model changed to llava:13b: this clip waits.", endpoint_fault=False)
        error.model_changed = True
        assert guard.charges(error) is False
    assert not guard.down
