"""send_until_decided: the one "409 → ask → send again" loop every CLI command uses."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import typer

from src.client.cli.decisions import in_projects, send_until_decided

pytestmark = pytest.mark.fast


def _answer(status: int, body: dict | None = None) -> MagicMock:
    r = MagicMock(status_code=status)
    r.json.return_value = body or {}
    r.text = str(body)
    return r


def _conflict(code: str, **details) -> MagicMock:
    return _answer(409, {"error": {"code": code, "message": f"{code}!", "details": details}})


def test_sent_again_with_the_answer() -> None:
    client = MagicMock()
    client.raw.side_effect = [_conflict("in_projects", assets_in_projects=2), _answer(200, {"ok": 1})]

    r = send_until_decided(client, "DELETE", "/v1/x", {"remove_from_projects": False},
                           answers={"in_projects": lambda d: {"remove_from_projects": True}}, failed="Nope")

    assert r.json() == {"ok": 1}
    assert [c.kwargs["json"] for c in client.raw.call_args_list] == [
        {"remove_from_projects": False}, {"remove_from_projects": True}]


def test_several_decisions_in_turn() -> None:
    client = MagicMock()
    client.raw.side_effect = [_conflict("confirm", count=3), _conflict("in_projects"), _answer(204)]

    send_until_decided(client, "DELETE", "/v1/x", {}, failed="Nope", answers={
        "confirm": lambda d: {"count": d["count"]}, "in_projects": lambda d: {"remove_from_projects": True}})

    assert client.raw.call_args.kwargs["json"] == {"count": 3, "remove_from_projects": True}


@pytest.mark.parametrize("reply", [_answer(500, {"error": {"message": "boom"}}), _conflict("unknown_code"),
                                   _answer(400, {"error": {"code": "in_projects", "message": "boom"}})])
def test_anything_else_fails(reply, capsys) -> None:
    client = MagicMock()
    client.raw.return_value = reply

    with pytest.raises(typer.Exit) as exit_:
        send_until_decided(client, "POST", "/v1/x", {}, answers={"in_projects": lambda d: {"y": 1}}, failed="Nope")

    assert exit_.value.exit_code == 1
    assert client.raw.call_count == 1


def test_the_same_question_again_fails_rather_than_looping() -> None:
    client = MagicMock()
    client.raw.return_value = _conflict("in_projects")

    with pytest.raises(typer.Exit) as exit_:
        send_until_decided(client, "POST", "/v1/x", {"remove_from_projects": True},
                           answers={"in_projects": lambda d: {"remove_from_projects": True}}, failed="Nope")

    assert exit_.value.exit_code == 1
    assert client.raw.call_count == 1


def test_in_projects_with_yes_stops_with_2() -> None:
    with pytest.raises(typer.Exit) as exit_:
        in_projects(what="they leave", yes=True)({"assets_in_projects": 1, "projects": []})
    assert exit_.value.exit_code == 2
