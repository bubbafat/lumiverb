# ruff: noqa: F811 — pytest fixtures are named as parameters
"""AI-assisted search, end to end (src/server/search/assist.py): a search that says so (assist:yes), in an account
that lets searches ask, searches a word naming a kind of thing with the things of that kind its AI machine lists,
kept for the next search. The machine is a real one on this computer (tests/ai_machine_server.py); everything else is
the real API and database. What the reply holds, and the words asked about: tests/test_search_assist.py."""

from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from tests.ai_machine_server import local_machine
from tests.test_ai_machines import QWEN, _add, _ai, _machine, _model, none  # noqa: F401
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_text_search import _clip, _describe, _library

pytestmark = pytest.mark.slow


def _listing(*things: str) -> tuple[int, dict, dict]:
    """A machine's answer listing these things."""
    reply = json.dumps({"also": list(things)})
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


@pytest.fixture
def pantry(env, none):
    """A library with tortilla chips, popcorn and a castle, and the account's machine describing them."""
    lib = _library(env)
    clips = {name: _clip(env, lib, f"Park/{name}.jpg") for name in ("chips", "popcorn", "castle")}
    _describe(env, clips["chips"], "A woven basket of tortilla chips on a white table")
    _describe(env, clips["popcorn"], "A striped bag of popcorn held up in front of a parade")
    _describe(env, clips["castle"], "A pink castle at night")
    with local_machine(_listing("popcorn", "tortilla chips", "ice cream")) as machine:
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
    assert _asked(machine) == 1
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
    machine.answer = _listing("popcorn")
    assert {i["asset_id"] for i in _search(env, lib, "nibbles")["items"]} == {clips["popcorn"]}
    assert _asked(machine) == 2


def test_a_word_already_particular_is_kept_as_nothing_more(env, pantry):
    lib, clips, machine = pantry
    machine.answer = _listing()
    said = _search(env, lib, "castle")
    assert {i["asset_id"] for i in said["items"]} == {clips["castle"]} and not said["understood"]
    _search(env, lib, "castle")
    assert _asked(machine) == 1  # kept: nothing more to it


def test_the_setting_is_an_admins_and_anyone_reads_it(env):
    from tests.test_archive_trash_safety import _key_with_role

    client, headers, *_ = env
    assert client.get("/v1/tenant/settings", headers=_key_with_role(env, "viewer")).json()["assisted_search"] is False
    r = client.patch("/v1/tenant/settings", json={"assisted_search": True}, headers=_key_with_role(env, "editor"))
    assert r.status_code == 403, r.text
