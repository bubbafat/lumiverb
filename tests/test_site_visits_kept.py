"""Site visits kept beyond nginx's 14 days: upkeep adds up each whole day of Lumiverb's copy of its sites' log
once, into the control database (counts and likely people, never an address: src/server/repository/site_visits.py),
and a server admin reads them (GET /v1/system/site-visits). Against a real database."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text

from src.server.api.routers import upkeep
from src.server.config import get_settings
from src.server.lifecycle import site_visits
from tests.test_first_run_setup import _client, control  # noqa: F401 (control: a fixture)
from tests.test_network_api import _signed_in
from tests.test_site_visits import IPHONE, MAC_CHROME, _line, _sites

pytestmark = pytest.mark.slow


def _at(day: date, clock: str) -> str:
    return f"{day:%d/%b/%Y}:{clock}"


def _log(today: date) -> str:
    first, person, browser = today - timedelta(days=3), today - timedelta(days=2), today - timedelta(days=1)
    lines = [
        # The log's first day: maybe partway, left out.
        _line("203.0.113.1", _at(first, "23:00:00"), "GET /assets/index-abc123.js"),
        _line("203.0.113.1", _at(first, "23:00:01"), "POST /v1/auth/demo"),
        # A person the day before yesterday; the people running it the same day.
        _line("203.0.113.5", _at(person, "10:15:00"), "GET /", referrer="https://example.org/"),
        _line("203.0.113.5", _at(person, "10:15:01"), "GET /assets/index-abc123.js"),
        _line("203.0.113.5", _at(person, "10:15:05"), "POST /v1/auth/demo"),
        _line("192.0.2.10", _at(person, "11:00:00"), "GET /assets/index-abc123.js"),
        _line("192.0.2.10", _at(person, "11:00:05"), "POST /v1/auth/demo"),
        # A browser alone yesterday.
        _line("198.51.100.30", _at(browser, "08:00:00"), "GET /assets/index-abc123.js", agent=MAC_CHROME),
        # Today's: not whole yet.
        _line("203.0.113.9", _at(today, "00:30:00"), "GET /assets/index-abc123.js", agent=IPHONE),
        _line("203.0.113.9", _at(today, "00:30:05"), "POST /v1/auth/demo", agent=IPHONE),
    ]
    return "".join(lines)


def test_upkeep_keeps_each_whole_day_once_and_a_server_admin_reads_them(control, tmp_path, monkeypatch):  # noqa: F811
    today = date.today()
    available = _sites(tmp_path)
    monkeypatch.setattr(site_visits, "SITES", str(available))
    monkeypatch.setattr(site_visits, "machine_addresses", lambda env=None: set())
    monkeypatch.setattr(site_visits, "ssh_logins", lambda since, env=None: {"192.0.2.10"})
    log = tmp_path / "log" / "sites.log"
    log.parent.mkdir()
    monkeypatch.setenv("LUMIVERB_SITE_LOG", str(log))
    get_settings.cache_clear()
    assert upkeep._keep_site_visits() is None, "no log here (a Mac's): nothing to add up"

    log.write_text(_log(today))
    assert upkeep._keep_site_visits() == {"days": 2, "people": 1}
    assert upkeep._keep_site_visits() == {"days": 0, "people": 0}, "a day kept is done"
    engine = create_engine(control)
    with engine.connect() as conn:
        columns = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name LIKE 'site_visit_%'"))}
    engine.dispose()
    assert not {"address", "ip", "user_agent"} & columns, "never who anyone is"

    with _client() as client:
        auth = _signed_in(client)
        said = client.get("/v1/system/site-visits?days=30", headers=auth)
        assert said.status_code == 200, said.text
        body = said.json()
    assert body["logged"] is True
    assert [d["day"] for d in body["days"]] == [str(today - timedelta(days=1)), str(today - timedelta(days=2))]
    yesterday, before = body["days"]
    assert yesterday["people"] == [] and yesterday["browsers"]["try.example.org"] == 1
    (person,) = before["people"]
    assert person["sites"] == ["try.example.org"] and person["device"] == "iPhone Safari"
    assert person["why"] == ["tried the demo x1", "followed a link from another of these sites"]
    assert before["browsers"]["try.example.org"] == 1, "the people running it left out"
