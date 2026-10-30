# ruff: noqa: F811 — pytest fixtures are named as parameters
"""A named person's other names, as the account's AI machine says them (src/server/search/person_nicknames.py):
what's taken from its reply, how a search understands them (understand.py), and end to end: a person named, upkeep
asking a machine (a real one on this computer, tests/ai_machine_server.py) once, and a search by another of their
names finding them, someone named by their own name first."""

from __future__ import annotations

import json

import pytest

from src.server.search import person_nicknames
from src.server.search.understand import PERSON, Person, understand
from tests.ai_machine_server import local_machine
from tests.test_ai_machines import QWEN, _add, _model, none  # noqa: F401
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_search_assisted import _allowed, _asked, _prompts
from tests.test_search_understood import _face, _named, _search
from tests.test_text_search import _clip, _describe, _library


def _nobody(_phrase: str) -> list:
    return []


@pytest.mark.fast
def test_what_a_reply_says_of_each_first_name():
    reply = 'Here: {"names": {"Giuseppe": ["Beppe", "Pino", "Peppe", "Joe"], "Verdi": ["V"], "Pat": []}}'
    assert person_nicknames.said(reply, "Giuseppe Verdi") == {"giuseppe": ["beppe", "pino", "peppe"]}  # MOST


@pytest.mark.fast
@pytest.mark.parametrize(("names", "kept"), [
    ({"Robert": ["Rob", "rob", "Bob", "Robert", "Horvick"]}, {"robert": ["rob", "bob"]}),  # each once, never its own
    ({"Robert": ["Bobby-Joe", "R2D2", "x", "the", "Bob Smith"]}, {"robert": ["bobby-joe"]}),  # one name of letters
    ({"Roberta": ["Bertie"]}, {}),  # not a word of the name
    ({"Robert": "Bob"}, {}),
])
def test_only_names_are_kept(names, kept):
    assert person_nicknames.said(json.dumps({"names": names}), "Robert Horvick") == kept


@pytest.mark.fast
@pytest.mark.parametrize("reply", ["Bob, Rob, Bobby", '{"nicknames": ["Bob"]}', '{"names": ["Bob"]}'])
def test_a_reply_that_isnt_the_json_asked_for(reply):
    with pytest.raises(ValueError):
        person_nicknames.said(reply, "Robert Horvick")


@pytest.mark.fast
def test_a_name_is_kept_by_the_name_so_a_rename_asks_again():
    assert person_nicknames.key("José  Núñez") == person_nicknames.key("jose nunez") == "nicknames:jose nunez"
    assert person_nicknames.key("Robert Horvick") != person_nicknames.key("Rob Horvick")


@pytest.mark.fast
def test_another_of_their_names_finds_them_and_their_own_ranks_first():
    giuseppe = Person("g", "Giuseppe Verdi", (("giuseppe", ("beppe", "pino")),))
    robert = Person("r", "Robert Horvick", (("robert", ("rob", "bob", "bobby")),))
    rob = Person("s", "Rob Smith")
    people = [giuseppe, robert, rob]
    (part,) = understand("Beppe", people, _nobody).parts
    assert (part.kind, part.person_ids, part.own_ids) == (PERSON, ("g",), ())
    assert "giuseppe" in part.also and "pino" in part.also  # their words match a clip's too
    assert understand("Pino Verdi", people, _nobody).parts[0].person_ids == ("g",)  # first then last
    (part,) = understand("Rob", people, _nobody).parts
    assert (part.person_ids, part.own_ids) == (("r", "s"), ("s",))  # Rob Smith by his own name
    assert understand("Beppe", [Person("g", "Giuseppe Verdi")], _nobody).parts == ()  # not asked yet: nobody


@pytest.mark.slow
def test_upkeep_asks_once_and_a_search_finds_them_by_another_of_their_names(env, none):
    client, headers, *_ = env
    lib = _library(env)
    clips = {name: _clip(env, lib, f"Family/{name}.jpg") for name in ("giuseppe", "rob", "robert")}
    for name in clips:
        _describe(env, clips[name], "people at a table")
    giuseppe = _named(env, "Giuseppe Verdi", [_face(env, clips["giuseppe"])])
    _named(env, "Rob Smith", [_face(env, clips["rob"])])
    _named(env, "Robert Horvick", [_face(env, clips["robert"])])

    def answer(sent):
        asked = sent["messages"][-1]["content"]
        asked = asked if isinstance(asked, str) else "".join(p.get("text", "") for p in asked)
        names = ({"Giuseppe": ["Beppe", "Pino"]} if "Giuseppe" in asked else
                 {"Robert": ["Rob", "Bob", "Bobby"]} if "Robert" in asked else {"Rob": ["Robert", "Robbie"]})
        reply = json.dumps({"names": names})
        return 200, {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]}, {}

    def upkeep() -> dict:
        r = client.post("/v1/upkeep", headers=headers)
        assert r.status_code == 200, r.text
        return r.json()["other_names"]

    with local_machine(answer) as machine:
        assert _add(env, name="Ollama", api_url=machine.url, jobs=["vision"]).status_code == 201
        assert _model(env, "vision", QWEN, redo=False).status_code == 200
        assert upkeep()["asked"] == 0 and _asked(machine) == 0, "only where the account lets searches ask"
        _allowed(env, True)
        try:
            assert upkeep()["asked"] == 3 and _asked(machine) == 3
            assert any("'Giuseppe Verdi'" in p for p in _prompts(machine))
            assert upkeep()["asked"] == 0 and _asked(machine) == 3, "kept: asked once"

            said = _search(env, lib, "Beppe")
            assert [i["asset_id"] for i in said["items"]] == [clips["giuseppe"]]
            assert said["understood"][0]["label"] == "Giuseppe Verdi"
            # Rob Smith by his own name, then Robert Horvick by another of his.
            assert [i["asset_id"] for i in _search(env, lib, "Rob")["items"]] == [clips["rob"], clips["robert"]]

            # Renamed: asked about again.
            r = client.patch(f"/v1/people/{giuseppe}", json={"display_name": "Joe Verdi"}, headers=headers)
            assert r.status_code == 200, r.text
            assert upkeep()["asked"] == 1
        finally:
            _allowed(env, False)
