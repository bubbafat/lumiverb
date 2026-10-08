"""The worker's AI machines for a job (Settings → AI; Robert's call, Oct 8).

Before sending a machine work, the worker checks it offers the job's model,
and tells the server what it found, per machine. Requests go to online
machines, each up to its own limit, least busy first. A machine that fails
a request is skipped (the item goes to another) and checked again a minute
later; only when no machine is left is it the endpoint's fault, which stops
the job without charging any clip.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.client.cli.ai_pool import MachinePool, PooledCaptionProvider
from src.client.workers.captions.base import CaptionError
from src.shared.vision_endpoint import VisionEndpointError

pytestmark = pytest.mark.fast

QWEN = "qwen3-vl:8b"
BRAIN = {"machine_id": "aim_brain", "name": "Brain", "api_url": "http://brain/v1", "api_key": "sk-b", "at_once": 2}
STUDIO = {"machine_id": "aim_studio", "name": "Studio", "api_url": "http://studio/v1", "api_key": "", "at_once": 4}


def _client(machines=(BRAIN, STUDIO), model=QWEN):
    client = MagicMock()
    client.get.return_value.json.return_value = {"job": "vision", "model": model, "machines": list(machines)}
    return client


def _offering(answers: dict):
    """list_models per URL: a tuple of models, or an exception."""
    def list_models(url, key=None, **_):
        answer = answers[url]
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)
    return patch("src.client.cli.ai_pool.list_models", side_effect=list_models)


def _statuses(client) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in client.post.call_args_list:
        path = c.args[0]
        if path.startswith("/v1/ai/machines/") and path.endswith("/status"):
            out.setdefault(path.split("/")[4], []).append(c.kwargs["json"])
    return out


def test_each_machine_is_checked_and_reported_before_work():
    client = _client()
    with _offering({"http://brain/v1": (QWEN,), "http://studio/v1": VisionEndpointError("Couldn't reach it.")}):
        pool = MachinePool(client, "vision")
        assert pool.check() is True
    client.get.assert_called_with("/v1/ai/jobs/vision")
    assert [m.name for m in pool.machines if m.online] == ["Brain"]
    assert pool.capacity() == 2
    statuses = _statuses(client)
    assert statuses["aim_brain"] == [{"online": True, "error": "", "models": [QWEN]}]
    assert statuses["aim_studio"] == [{"online": False, "error": "Couldn't reach it.", "models": []}]


def test_a_machine_without_the_model_is_skipped_and_says_why():
    client = _client()
    with _offering({"http://brain/v1": (QWEN,), "http://studio/v1": ("llava:13b",)}):
        pool = MachinePool(client, "vision")
        pool.check()
    studio = _statuses(client)["aim_studio"][-1]
    assert studio["online"] is False and "doesn't offer qwen3-vl:8b" in studio["error"]
    assert studio["models"] == ["llava:13b"]


@pytest.mark.parametrize(("machines", "model", "says"), [
    ((BRAIN,), "", "No model is chosen for descriptions & text"),
    ((), QWEN, "No machine does descriptions & text"),
])
def test_nothing_to_work_with_says_what_to_do_in_settings(machines, model, says):
    pool = MachinePool(_client(machines, model), "vision")
    with _offering({}):
        assert pool.check() is False
    assert pool.down and says in pool.error and "Settings → AI" in pool.error


def test_none_online_says_why_for_each():
    client = _client()
    with _offering({"http://brain/v1": VisionEndpointError("no answer"), "http://studio/v1": VisionEndpointError("refused")}):
        pool = MachinePool(client, "vision")
        assert pool.check() is False
    assert "Brain: no answer" in pool.error and "Studio: refused" in pool.error


def test_requests_go_to_online_machines_each_up_to_its_limit_least_busy_first():
    client = _client()
    with _offering({"http://brain/v1": (QWEN,), "http://studio/v1": (QWEN,)}):
        pool = MachinePool(client, "vision")
        pool.check()
    assert pool.capacity() == 6
    taken = [pool.acquire() for _ in range(6)]
    assert sorted(m.name for m in taken).count("Brain") == 2
    assert sorted(m.name for m in taken).count("Studio") == 4
    assert taken[0].name == "Brain"  # both idle: the first; then whichever is least busy for its size
    # Full: the next waits for a slot.
    got = []
    t = threading.Thread(target=lambda: got.append(pool.acquire()))
    t.start()
    time.sleep(0.2)
    assert got == []
    pool.release(taken[0])
    t.join(2)
    assert got and got[0].name == "Brain"


def test_a_machine_that_fails_is_skipped_and_checked_again_a_minute_later():
    client = _client()
    clock = {"now": 0.0}
    answers = {"http://brain/v1": (QWEN,), "http://studio/v1": (QWEN,)}
    with _offering(answers) as asked:
        pool = MachinePool(client, "vision", clock=lambda: clock["now"])
        pool.check()
        studio = next(m for m in pool.machines if m.name == "Studio")
        pool.fault(studio, CaptionError("500 out of memory", endpoint_fault=True))
        assert not studio.online and pool.capacity() == 2
        assert _statuses(client)["aim_studio"][-1] == {"online": False, "error": "500 out of memory", "models": []}
        calls = asked.call_count
        clock["now"] = 30.0
        pool.release(pool.acquire())
        assert asked.call_count == calls  # not yet
        clock["now"] = 61.0
        pool.release(pool.acquire())
        assert asked.call_count == calls + 1 and studio.online  # back
        assert _statuses(client)["aim_studio"][-1]["online"] is True


def test_the_pooled_provider_moves_an_item_to_another_machine_when_one_fails():
    client = _client()
    with _offering({"http://brain/v1": (QWEN,), "http://studio/v1": (QWEN,)}):
        pool = MachinePool(client, "vision")
        pool.check()
    providers = {}

    def make(machine):
        p = MagicMock()
        if machine.name == "Brain":
            p.describe.side_effect = CaptionError("500 out of memory", endpoint_fault=True)
        else:
            p.describe.return_value = {"description": "a cat", "tags": ["cat"]}
        providers[machine.name] = p
        return p

    provider = PooledCaptionProvider(pool, make)
    assert provider.describe("a.jpg") == {"description": "a cat", "tags": ["cat"]}
    assert providers["Brain"].describe.call_count == 1
    assert [m.name for m in pool.machines if m.online] == ["Studio"]
    assert all(m.busy == 0 for m in pool.machines)
    # The next goes straight to the machine that works.
    provider.describe("b.jpg")
    assert providers["Brain"].describe.call_count == 1


def test_an_items_own_failure_isnt_moved_or_held_against_the_machine():
    client = _client((BRAIN,))
    with _offering({"http://brain/v1": (QWEN,)}):
        pool = MachinePool(client, "vision")
        pool.check()
    bad = MagicMock()
    bad.extract_text.side_effect = CaptionError("unparseable answer", endpoint_fault=False)
    provider = PooledCaptionProvider(pool, lambda m: bad)
    with pytest.raises(CaptionError) as e:
        provider.extract_text("a.jpg")
    assert e.value.endpoint_fault is False
    assert pool.machines[0].online and pool.machines[0].busy == 0


def test_with_no_machine_left_its_the_endpoints_fault():
    client = _client((BRAIN,))
    with _offering({"http://brain/v1": (QWEN,)}):
        pool = MachinePool(client, "vision")
        pool.check()
    failing = MagicMock()
    failing.describe.side_effect = CaptionError("connection refused", endpoint_fault=True)
    provider = PooledCaptionProvider(pool, lambda m: failing)
    with pytest.raises(CaptionError) as e:
        provider.describe("a.jpg")
    assert e.value.endpoint_fault is True and "Brain" in str(e.value)
    assert pool.down


def test_one_provider_per_machine():
    client = _client()
    with _offering({"http://brain/v1": (QWEN,), "http://studio/v1": (QWEN,)}):
        pool = MachinePool(client, "vision")
        pool.check()
    made = []

    def make(machine):
        made.append(machine.name)
        p = MagicMock()
        p.describe.return_value = {"description": "", "tags": []}
        return p

    provider = PooledCaptionProvider(pool, make)
    for _ in range(5):
        provider.describe("a.jpg")
    assert sorted(set(made)) == sorted(made)
