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


def test_an_item_tries_each_machine_once_then_its_the_endpoints_fault():
    """Machines that list the model but fail every request (a hung GPU) come
    back online after their recheck: the item must not go round forever."""
    client = _client()
    clock = {"now": 0.0}
    with _offering({"http://brain/v1": (QWEN,), "http://studio/v1": (QWEN,)}):
        pool = MachinePool(client, "vision", clock=lambda: clock["now"])
        pool.check()
        calls = []

        def make(machine):
            p = MagicMock()

            def describe(path):
                calls.append(machine.name)
                assert len(calls) < 10, "an item went round the machines forever"
                clock["now"] += 61.0  # each failure takes a while: the others are due a recheck
                raise CaptionError("timed out", endpoint_fault=True)
            p.describe.side_effect = describe
            return p

        with pytest.raises(CaptionError) as e:
            PooledCaptionProvider(pool, make).describe("a.jpg")
    assert e.value.endpoint_fault is True
    assert sorted(calls) == ["Brain", "Studio"]


def test_while_a_machine_is_being_checked_the_job_isnt_given_up():
    """One thread checks the only machine again; another wanting a slot waits
    for that check instead of declaring the job down."""
    client = _client((BRAIN,))
    clock = {"now": 0.0}
    with _offering({"http://brain/v1": (QWEN,)}):
        pool = MachinePool(client, "vision", clock=lambda: clock["now"])
        pool.check()
    brain = pool.machines[0]
    pool.fault(brain, "timed out")
    clock["now"] = 61.0
    gate = threading.Event()

    def slow_models(*_a, **_k):
        gate.wait(5)
        return [QWEN]

    got: list = []
    with patch("src.client.cli.ai_pool.list_models", side_effect=slow_models):
        first = threading.Thread(target=lambda: got.append(pool.acquire()))
        first.start()
        for _ in range(50):
            if brain.checking:
                break
            time.sleep(0.01)
        second = threading.Thread(target=lambda: got.append(pool.acquire()))
        second.start()
        time.sleep(0.3)
        assert got == []  # waiting for the check, not giving up
        gate.set()
        first.join(5)
        second.join(5)
    assert [m.name if m else None for m in got] == ["Brain", "Brain"]


def test_a_failed_item_says_which_machine_served_it():
    client = _client((BRAIN,))
    with _offering({"http://brain/v1": (QWEN,)}):
        pool = MachinePool(client, "vision")
        pool.check()
    bad = MagicMock()
    bad.describe.side_effect = CaptionError("unparseable answer", endpoint_fault=False)
    with pytest.raises(CaptionError) as e:
        PooledCaptionProvider(pool, lambda m: bad).describe("a.jpg")
    assert e.value.machine is pool.machines[0]


# ---------------------------------------------------------------------------
# Transcripts: the built-in Whisper, and servers that name a model their way
# ---------------------------------------------------------------------------

BUILT_IN = {"machine_id": "aim_self", "name": "Built in", "api_url": "", "api_key": "", "at_once": 1, "built_in": True}
SPEACHES = {"machine_id": "aim_sp", "name": "Speaches", "api_url": "http://speaches/v1", "api_key": "", "at_once": 2,
            "built_in": False}


def _transcripts(machines=(BUILT_IN, SPEACHES), model="small"):
    client = MagicMock()
    client.get.return_value.json.return_value = {"job": "transcripts", "model": model, "machines": list(machines)}
    return client


def test_the_built_in_whisper_is_checked_on_this_computer_not_asked():
    from src.shared.whisper_models import BUILT_IN_MODELS

    client = _transcripts((BUILT_IN,))
    with _offering({}) as asked:
        pool = MachinePool(client, "transcripts")
        assert pool.check() is True
    asked.assert_not_called()
    built_in = pool.machines[0]
    assert built_in.built_in and built_in.online and built_in.serves == "small"
    assert _statuses(client)["aim_self"] == [{"online": True, "error": "", "models": BUILT_IN_MODELS}]


def test_the_built_in_whisper_without_faster_whisper_or_the_model_says_why():
    client = _transcripts((BUILT_IN,))
    with patch("src.client.workers.transcripts.local.unavailable", return_value="faster-whisper isn't installed."):
        pool = MachinePool(client, "transcripts")
        assert pool.check() is False
    assert "faster-whisper isn't installed" in _statuses(client)["aim_self"][-1]["error"]
    pool = MachinePool(_transcripts((BUILT_IN,), model="whisper-1"), "transcripts")
    assert pool.check() is False
    assert "doesn't know whisper-1" in pool.machines[0].error


def test_a_server_is_asked_for_the_model_by_the_name_it_lists():
    client = _transcripts((SPEACHES,), model="small")
    with _offering({"http://speaches/v1": ("Systran/faster-whisper-small", "kokoro")}):
        pool = MachinePool(client, "transcripts")
        assert pool.check() is True
    assert pool.machines[0].serves == "Systran/faster-whisper-small"
    with _offering({"http://speaches/v1": ("Systran/faster-whisper-medium",)}):
        assert pool.recheck() is False
    assert "doesn't offer small" in pool.machines[0].error


def test_transcripts_go_to_each_machine_as_it_names_the_model_and_move_on_when_one_fails():
    from src.client.cli.ai_pool import PooledTranscriber
    from src.client.workers.transcripts.base import Heard, Segment, TranscriptError

    client = _transcripts()
    with _offering({"http://speaches/v1": ("Systran/faster-whisper-small",)}):
        pool = MachinePool(client, "transcripts")
        pool.check()
    made = {}

    def make(machine):
        t = MagicMock()
        made[machine.name] = (machine.serves, t)
        if machine.built_in:
            t.transcribe.side_effect = TranscriptError("Couldn't load small: no space left", endpoint_fault=True)
        else:
            t.transcribe.return_value = Heard([Segment(0.0, 1.0, " hi")], "en")
        return t

    transcriber = PooledTranscriber(pool, make)
    assert transcriber.transcribe("speech.wav") == Heard([Segment(0.0, 1.0, " hi")], "en")
    assert made["Built in"][0] == "small" and made["Speaches"][0] == "Systran/faster-whisper-small"
    assert [m.name for m in pool.machines if m.online] == ["Speaches"]
    assert _statuses(client)["aim_self"][-1]["error"] == "Couldn't load small: no space left"

    # A clip's own failure says which machine heard it (with the built-in out, no other to try).
    made["Speaches"][1].transcribe.side_effect = TranscriptError("audio too short", endpoint_fault=False)
    with pytest.raises(TranscriptError) as e:
        transcriber.transcribe("b.wav")
    assert e.value.endpoint_fault is False and e.value.machine.name == "Speaches"
    assert [m.name for m in pool.machines if m.online] == ["Speaches"]  # not held against it
    # None left: the machines' fault.
    made["Speaches"][1].transcribe.side_effect = TranscriptError("connection refused", endpoint_fault=True)
    with pytest.raises(TranscriptError) as e:
        transcriber.transcribe("c.wav")
    assert e.value.endpoint_fault is True and pool.down


def test_a_clip_one_server_refuses_goes_to_the_others_first():
    """A server may refuse what another takes (nginx's body limit in front of
    it, say); the clip is the clip's only once every machine has refused it."""
    from src.client.cli.ai_pool import PooledTranscriber
    from src.client.workers.transcripts.base import Heard, TranscriptError

    client = _transcripts()
    with _offering({"http://speaches/v1": ("Systran/faster-whisper-small",)}):
        pool = MachinePool(client, "transcripts")
        pool.check()
    calls = []

    def make(machine):
        t = MagicMock()

        def transcribe(wav):
            calls.append(machine.name)
            if machine.name == "Speaches" or wav == "bad.wav":
                raise TranscriptError(f"{machine.name} answered 413", endpoint_fault=False)
            return Heard()
        t.transcribe.side_effect = transcribe
        return t

    transcriber = PooledTranscriber(pool, make)
    pool.machines[0].busy = 1  # the built-in is busy: the server is asked first
    got = []
    worker = threading.Thread(target=lambda: got.append(transcriber.transcribe("big.wav")))
    worker.start()
    time.sleep(0.2)
    pool.release(pool.machines[0])
    worker.join(5)
    assert got == [Heard()] and calls == ["Speaches", "Built in"]
    assert all(m.online for m in pool.machines)
    with pytest.raises(TranscriptError) as e:
        transcriber.transcribe("bad.wav")
    assert e.value.endpoint_fault is False and sorted(calls[2:]) == ["Built in", "Speaches"]


# ---------------------------------------------------------------------------
# Checked again while requests are in flight (the scheduler checks every minute)
# ---------------------------------------------------------------------------


def test_checking_again_keeps_what_requests_in_flight_hold():
    client = _client()
    with _offering({"http://brain/v1": (QWEN,), "http://studio/v1": (QWEN,)}):
        pool = MachinePool(client, "vision")
        pool.check()
        held = [pool.acquire() for _ in range(6)]  # every slot: 2 on the brain, 4 on the studio
        assert all(held)
        pool.check()
    # Still full: a new request waits rather than overloading a machine.
    assert {m.name: m.busy for m in pool.machines} == {"Brain": 2, "Studio": 4}
    for m in held:
        pool.release(m)
    assert {m.name: m.busy for m in pool.machines} == {"Brain": 0, "Studio": 0}


def test_a_machine_whose_address_changed_is_checked_again_and_gets_a_new_provider():
    client = _client()
    with _offering({"http://brain/v1": (QWEN,), "http://studio/v1": (QWEN,), "http://brain2/v1": (QWEN,)}):
        pool = MachinePool(client, "vision")
        pool.check()
        made = []

        def make(machine):
            made.append(machine.api_url)
            p = MagicMock()
            p.describe.return_value = {"description": "", "tags": []}
            return p

        provider = PooledCaptionProvider(pool, make)
        provider.describe("a.jpg")
        client.get.return_value.json.return_value = {"job": "vision", "model": QWEN, "machines": [
            {**BRAIN, "api_url": "http://brain2/v1"}]}
        pool.check()
        assert [m.api_url for m in pool.machines] == ["http://brain2/v1"] and pool.machines[0].online
        provider.describe("b.jpg")
    assert made[-1] == "http://brain2/v1"


def test_a_machine_no_longer_listed_is_dropped():
    client = _client()
    with _offering({"http://brain/v1": (QWEN,), "http://studio/v1": (QWEN,)}):
        pool = MachinePool(client, "vision")
        pool.check()
        client.get.return_value.json.return_value = {"job": "vision", "model": QWEN, "machines": [BRAIN]}
        pool.check()
    assert [m.name for m in pool.machines] == ["Brain"] and pool.capacity() == 2


def test_a_new_model_sends_nothing_to_a_machine_until_its_checked_with_it():
    """Review round 2: work started right after a model change was recorded as
    the new model's while the machine still served the old one."""
    llava = "llava:13b"
    client = _client()
    with _offering({"http://brain/v1": (QWEN, llava), "http://studio/v1": (QWEN,)}) as listed:
        pool = MachinePool(client, "vision")
        pool.check()
        assert all(m.online and m.serves == QWEN for m in pool.machines)
        client.get.return_value.json.return_value = {"job": "vision", "model": llava, "machines": [BRAIN, STUDIO]}
        pool.load()  # the recheck hasn't run yet
        assert pool.model == llava
        assert not any(m.online or m.serves for m in pool.machines)
        listed.reset_mock()
        machine = pool.acquire()  # checks them with the new model first
        assert listed.call_count == 2
        assert machine is not None and machine.name == "Brain" and machine.serves == llava
        pool.release(machine)
    studio = next(m for m in pool.machines if m.name == "Studio")
    assert not studio.online and llava in studio.error


def test_work_started_with_one_model_isnt_served_by_the_next():
    """Review round 3: a job that took its model before a change, reaching a
    machine checked with the new one, was answered by the new model and
    saved as the old one's. It waits instead, uncharged."""
    llava = "llava:13b"
    client = _client(machines=(BRAIN,))
    with _offering({"http://brain/v1": (QWEN, llava)}):
        pool = MachinePool(client, "vision")
        pool.check()
        made = []

        def make(machine):
            made.append(machine.serves)
            p = MagicMock()
            p.describe.return_value = {"description": "", "tags": []}
            return p

        provider = PooledCaptionProvider(pool, make)  # made for qwen
        client.get.return_value.json.return_value = {"job": "vision", "model": llava, "machines": [BRAIN]}
        pool.check()
        with pytest.raises(CaptionError) as raised:
            provider.describe("a.jpg")
    assert raised.value.model_changed is True and raised.value.endpoint_fault is False
    assert made == []  # nothing asked of the new model for it
    assert all(m.busy == 0 for m in pool.machines)


def test_a_check_for_a_model_no_longer_chosen_is_left_out():
    """Review round 3: a check that started before a model change mustn't
    mark the machine as serving the new model with the old one's answer."""
    llava = "llava:13b"
    client = _client(machines=(BRAIN,))
    pool = MachinePool(client, "vision")

    def list_models(url, key=None, **_):
        # The model changes while the machine is being asked.
        client.get.return_value.json.return_value = {"job": "vision", "model": llava, "machines": [BRAIN]}
        pool.load()
        return [QWEN]

    with patch("src.client.cli.ai_pool.list_models", side_effect=list_models):
        pool.load()
        pool._check(pool.machines[0])
    brain = pool.machines[0]
    assert not brain.online and brain.serves == "" and brain.checked_at is None  # checked again, with llava


# ---------------------------------------------------------------------------
# Video work comes first on a shared GPU (Robert, Oct 9)
# ---------------------------------------------------------------------------


def test_a_machine_sharing_the_gpu_takes_fewer_requests_while_video_is_decoded():
    client = _client(machines=({**BRAIN, "shares_gpu": True}, STUDIO))
    with _offering({"http://brain/v1": (QWEN,), "http://studio/v1": (QWEN,)}):
        pool = MachinePool(client, "vision")
        pool.check()
    assert pool.capacity() == 6
    pool.set_gpu_hold(1)
    assert pool.capacity() == 5  # the brain takes one fewer
    held = [pool.acquire() for _ in range(5)]
    assert sorted(m.name for m in held).count("Brain") == 1
    pool.set_gpu_hold(5)
    assert pool.capacity() == 4  # down to none on the brain; the studio keeps going
    for m in held:
        pool.release(m)
    pool.set_gpu_hold(0)
    assert pool.capacity() == 6


def test_the_built_in_machine_always_shares_the_gpu():
    built_in = {"machine_id": "aim_bi", "name": "Built in", "api_url": "", "api_key": "", "at_once": 1,
                "built_in": True}
    client = _client(machines=(built_in,))
    pool = MachinePool(client, "transcripts")
    pool.load()
    assert pool.machines[0].shares_gpu is True
