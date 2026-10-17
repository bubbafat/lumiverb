"""`lumiverb settings key-idle-days`: ending keys people made once nobody has
used them for so many days, off until set; fewer days says which keys would
stop working and asks (409 keys_would_end), and `settings show` says it."""

from __future__ import annotations

import pytest

from tests.test_cli_archive_trash import _answer, _client, _run

pytestmark = pytest.mark.fast


@pytest.mark.parametrize("arg,value,shown", [("90", 90, "ended after 90 days unused"), ("1", 1, "after 1 day unused"),
                                             ("off", None, "kept until revoked"), ("OFF", None, "until revoked")])
def test_settings_key_idle_days(arg, value, shown):
    client = _client()
    client.raw.return_value = _answer(200, {"key_idle_days": value})
    result = _run(client, "settings", "key-idle-days", arg)
    assert result.exit_code == 0, result.output
    client.raw.assert_called_once_with("PATCH", "/v1/tenant/settings",
                                       json={"key_idle_days": value, "confirm_end_keys": False})
    assert shown in result.output


@pytest.mark.parametrize("answer,calls", [("y", 2), ("n", 1)])
def test_fewer_days_names_the_keys_and_asks(answer, calls):
    client = _client()
    would_end = _answer(409, {"error": {"code": "keys_would_end", "message": "m", "details": {
        "key_idle_days": 30, "keys": [{"key_id": "key_1", "label": "old script", "role": "editor", "last_used_at": None},
                                      {"key_id": "key_2", "label": None, "role": "viewer", "last_used_at": None}]}}})
    client.raw.side_effect = [would_end, _answer(200, {"key_idle_days": 30})]
    result = _run(client, "settings", "key-idle-days", "30", input=f"{answer}\n")
    assert result.exit_code == 0, result.output
    assert "2 keys unused for longer would stop working within minutes: old script, key_2" in " ".join(
        result.output.split())
    assert client.raw.call_count == calls
    if calls == 2:
        assert client.raw.call_args.kwargs["json"] == {"key_idle_days": 30, "confirm_end_keys": True}


@pytest.mark.parametrize("arg", ["0", "3651", "-1", "2.5", "ninety", "٣"])
def test_settings_key_idle_days_refuses_nonsense(arg):
    client = _client()
    result = _run(client, "settings", "key-idle-days", "--", arg)
    assert result.exit_code == 2, result.output
    client.raw.assert_not_called()


def test_settings_show_says_what_happens_to_unused_keys():
    client = _client(**{"/v1/tenant/settings": {"key_idle_days": 90}})
    assert "Unused keys: ended after 90 days unused" in _run(client, "settings", "show").output
    client = _client(**{"/v1/tenant/settings": {"key_idle_days": None}})
    assert "Unused keys: kept until revoked" in _run(client, "settings", "show").output
