# ruff: noqa: F811 — pytest fixtures are named as parameters
"""AI-assisted search, end to end (src/server/search/assist.py): a search that says so (assist:yes), in an account
that lets searches ask, searches the words naming a kind of thing with the things of that kind its AI machine lists,
its words asked about together and kept for the next search, and asked once by a search's requests at once. The
machine is a real one on this computer (tests/ai_machine_server.py); everything else is the real API and database.
What the reply holds, and the words asked about: tests/test_search_assist.py."""

from __future__ import annotations

import json
import threading
import time
import uuid
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session

from tests.ai_machine_server import local_machine
from tests.test_ai_machines import QWEN, _add, _ai, _machine, _model, none  # noqa: F401
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_text_search import _clip, _describe, _library

pytestmark = pytest.mark.slow


def _listing(**kinds: list[str]) -> tuple[int, dict, dict]:
    """A machine's answer naming these kinds (a kind of two words: theme_park), each listing its things."""
    reply = json.dumps({"kinds": {kind.replace("_", " "): things for kind, things in kinds.items()}})
    return 200, {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]}, {}


def _allowed(env, on: bool) -> None:
    client, headers, *_ = env
    r = client.patch("/v1/tenant/settings", json={"assisted_search": on}, headers=headers)
    assert r.status_code == 200 and r.json()["assisted_search"] is on, r.text


def _search(env, lib: str, words: str, *, assist: bool = True) -> dict:
    client, headers, *_ = env
    f = f"f=query:{quote(words)}&f=library:{lib}" + ("&f=assist:yes" if assist else "")
    r = client.get(f"/v1/query?{f}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _asked(machine) -> int:
    return sum(1 for method, path in machine.asked if method == "POST" and path.endswith("/chat/completions"))


def _prompts(machine) -> list[str]:
    """What the machine was asked, each prompt's text."""
    out = []
    for (method, path), body in zip(machine.asked, machine.sent, strict=True):
        if method == "POST" and path.endswith("/chat/completions"):
            content = json.loads(body)["messages"][-1]["content"]
            out.append(content if isinstance(content, str) else "".join(p.get("text", "") for p in content))
    return out


@pytest.fixture
def pantry(env, none):
    """A library with tortilla chips, popcorn, a castle, a roller coaster and two trucks, and the account's machine
    describing them."""
    lib = _library(env)
    clips = {name: _clip(env, lib, f"Park/{name}.jpg")
             for name in ("chips", "popcorn", "castle", "coaster", "red_truck", "white_truck")}
    _describe(env, clips["chips"], "A woven basket of tortilla chips on a white table")
    _describe(env, clips["popcorn"], "A striped bag of popcorn held up in front of a parade")
    _describe(env, clips["castle"], "A pink castle at night")
    _describe(env, clips["coaster"], "A wooden roller coaster against a blue sky")
    _describe(env, clips["red_truck"], "A red truck parked by a fence")
    _describe(env, clips["white_truck"], "A white truck parked by a fence")
    with local_machine(_listing(snacks=["popcorn", "tortilla chips", "ice cream"])) as machine:
        assert _add(env, name="Ollama", api_url=machine.url, jobs=["vision"]).status_code == 201
        assert _model(env, "vision", QWEN, redo=False).status_code == 200
        _allowed(env, True)
        try:
            yield lib, clips, machine
        finally:
            _allowed(env, False)


def test_a_kind_of_thing_finds_the_things_of_that_kind(env, pantry):
    lib, clips, machine = pantry
    said = _search(env, lib, "snacks")
    assert {i["asset_id"] for i in said["items"]} == {clips["chips"], clips["popcorn"]}
    assert {"kind": "also", "words": "snacks", "label": "popcorn, tortilla chips, ice cream"} in said["understood"]
    assert _asked(machine) == 1 and "'snacks'" in _prompts(machine)[0]
    # Kept: the next search asks nothing.
    assert {i["asset_id"] for i in _search(env, lib, "snacks")["items"]} == {clips["chips"], clips["popcorn"]}
    assert _asked(machine) == 1


def test_only_when_the_search_says_so_and_the_account_lets_it(env, pantry):
    lib, clips, machine = pantry
    assert _search(env, lib, "treats", assist=False)["items"] == []  # local: the words alone
    _allowed(env, False)
    said = _search(env, lib, "treats")  # asked for, but the account doesn't let searches ask
    assert said["items"] == [] and not said["understood"]
    assert _asked(machine) == 0


def test_quoted_words_are_never_asked_about(env, pantry):
    lib, _, machine = pantry
    assert _search(env, lib, '"goodies"')["items"] == []
    assert _asked(machine) == 0


def test_a_machine_that_cant_answer_leaves_the_words_and_is_asked_again(env, pantry):
    lib, clips, machine = pantry
    machine.answer = (500, {"error": {"message": "busy"}}, {})
    said = _search(env, lib, "nibbles")
    assert said["items"] == [] and not said["understood"]
    machine.answer = _listing(nibbles=["popcorn"])
    assert {i["asset_id"] for i in _search(env, lib, "nibbles")["items"]} == {clips["popcorn"]}
    assert _asked(machine) == 2


def test_a_word_already_particular_is_kept_as_nothing_more(env, pantry):
    lib, clips, machine = pantry
    machine.answer = _listing()
    said = _search(env, lib, "castle")
    assert {i["asset_id"] for i in said["items"]} == {clips["castle"]} and not said["understood"]
    _search(env, lib, "castle")
    assert _asked(machine) == 1  # kept: nothing more to it


def test_a_searchs_words_are_asked_about_together_once(env, pantry):
    """Robert, Oct 29: the whole phrase, so "theme park" is a kind where "theme" and "park" alone aren't."""
    lib, clips, machine = pantry
    machine.answer = _listing(theme_park=["roller coaster", "carousel"])
    said = _search(env, lib, "disney theme park")
    assert clips["coaster"] in {i["asset_id"] for i in said["items"]}
    assert {"kind": "also", "words": "theme park", "label": "roller coaster, carousel"} in said["understood"]
    assert len(_prompts(machine)) == 1 and "'disney theme park'" in _prompts(machine)[0]


def test_a_kinds_things_stand_for_its_last_word_and_the_rest_is_still_asked_for(env, pantry):
    lib, clips, machine = pantry
    machine.answer = _listing(red_car=["truck"])  # a machine that took "red" for part of the kind
    found = [i["asset_id"] for i in _search(env, lib, "red car")["items"]]
    # A truck is a car; the red one is all the search asks for, the white one only a car.
    assert found.index(clips["red_truck"]) < found.index(clips["white_truck"])


def test_a_searchs_requests_at_once_ask_once(env, pantry):
    """Robert, Oct 29: its results and its filters' counts, asked for together, ask the machine once: the second
    waits for the first's answer."""
    client, headers, *_ = env
    lib, clips, machine = pantry
    machine.answer = _listing(treats=["popcorn"])
    machine.delay = 2.0
    f = f"f=query:treats&f=library:{lib}&f=assist:yes"
    answers: dict[str, dict] = {}

    def get(path: str) -> None:
        r = client.get(f"{path}?{f}", headers=headers)
        assert r.status_code == 200, r.text
        answers[path] = r.json()

    first = threading.Thread(target=get, args=("/v1/query",))
    first.start()
    deadline = time.monotonic() + 10
    while not _asked(machine) and time.monotonic() < deadline:  # the first is asking now
        time.sleep(0.02)
    second = threading.Thread(target=get, args=("/v1/assets/facets",))
    second.start()
    first.join(30)
    second.join(30)
    assert set(answers) == {"/v1/query", "/v1/assets/facets"}
    assert _asked(machine) == 1
    assert {i["asset_id"] for i in answers["/v1/query"]["items"]} == {clips["popcorn"]}


def test_searches_asking_about_the_same_words_in_any_order_each_ask_once(env):
    """Requests asking about several searches' words at once, in different orders, each words asked once and none
    waiting on another forever (those waiting hold only shared locks)."""
    from src.server.search import assist

    model = f"model-{uuid.uuid4().hex[:8]}"
    asked: list[str] = []
    lock = threading.Lock()

    def ask(prompt: str) -> str:
        words = prompt.split("typed: '", 1)[1].split("'", 1)[0]
        with lock:
            asked.append(words)
        time.sleep(0.3)
        return json.dumps({"kinds": {"snacks": ["popcorn"]}})

    phrases = ["snacks one", "snacks two", "snacks three"]
    engine = create_engine(env[5])
    results: list[dict] = []
    errors: list[BaseException] = []

    def search(order: list[str]) -> None:
        try:
            with Session(engine) as session:
                results.append(assist.kinds(session, order, model, ask))
        except BaseException as e:  # noqa: BLE001 — said by the test
            errors.append(e)

    orders = [phrases[i:] + phrases[:i] for i in range(3)] + [list(reversed(phrases))]
    threads = [threading.Thread(target=search, args=(order,)) for order in orders]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    engine.dispose()
    assert not errors and not any(t.is_alive() for t in threads)
    assert sorted(asked) == sorted(phrases)  # each once
    assert all(r == {p: {"snacks": ["popcorn"]} for p in phrases} for r in results)


def test_the_setting_is_an_admins_and_anyone_reads_it(env):
    from tests.test_archive_trash_safety import _key_with_role

    client, headers, *_ = env
    assert client.get("/v1/tenant/settings", headers=_key_with_role(env, "viewer")).json()["assisted_search"] is False
    r = client.patch("/v1/tenant/settings", json={"assisted_search": True}, headers=_key_with_role(env, "editor"))
    assert r.status_code == 403, r.text
