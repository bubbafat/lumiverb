# ruff: noqa: F811 — pytest fixtures are named as parameters
"""Visits (ADR-025): what a public page's visitors do, counted on the server without keeping who they are
(src/server/visits.py, src/server/repository/visits.py). Through the real API and database: a visitor is
told apart by their user agent here, as every test client has the same address."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from src.server import visits
from tests.test_analysis_proxy_api import _ingest, _upload, env  # noqa: F401 — the shared server fixture

pytestmark = pytest.mark.slow

ANNA = {"User-Agent": "Mozilla/5.0 (Macintosh) Safari/605.1.15 anna"}
BEN = {"User-Agent": "Mozilla/5.0 (iPhone) Safari/604.1 ben"}
CRAWLER = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}


class Clock:
    """The visits' time, moved by the test."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def later(self, **delta) -> None:
        self.at += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch):
    c = Clock(datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0))
    monkeypatch.setattr(visits, "now", c)
    return c


@pytest.fixture
def shared(env):
    """A public project of a photo and a video that plays."""
    client, headers, *_ = env
    photo = _ingest(env, "Visits/beach.jpg", "image")
    video = _ingest(env, "Visits/waves.mov", "video")
    assert _upload(env, video).status_code == 200
    r = client.post("/v1/projects", json={"name": "Beach day", "asset_ids": [photo, video], "visibility": "public"},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["project_id"], photo, video


def _seen(env, project_id: str, who: dict, came_from: str | None = None) -> None:
    client, *_ = env
    params = {"from": came_from} if came_from else {}
    r = client.get(f"/v1/public/projects/{project_id}", params=params, headers=who)
    assert r.status_code == 200, r.text


def _open(env, project_id: str, clip: str, who: dict) -> None:
    client, *_ = env
    assert client.get(f"/v1/assets/{clip}", params={"public_project_id": project_id},
                      headers=who).status_code == 200


def _play(env, project_id: str, clip: str, who: dict) -> None:
    client, *_ = env
    r = client.get(f"/v1/assets/{clip}/playback", params={"public_project_id": project_id}, headers=who)
    assert r.status_code == 200, r.text


def _report(env, project_id: str, headers: dict | None = None, **params) -> dict:
    client, admin, *_ = env
    r = client.get(f"/v1/projects/{project_id}/visits", params=params, headers=headers or admin)
    assert r.status_code == 200, r.text
    return r.json()


def _totals(report: dict, action: str) -> tuple[int, int]:
    return report["totals"][action]["visitors"], report["totals"][action]["times"]


def test_a_visitor_doing_it_again_within_half_an_hour_counts_once(env, shared, clock):
    project_id, photo, video = shared
    _seen(env, project_id, ANNA)
    clock.later(minutes=10)
    _seen(env, project_id, ANNA)  # refreshed: the same view
    _seen(env, project_id, BEN)
    assert _totals(_report(env, project_id), "view") == (2, 2)
    clock.later(minutes=31)
    _seen(env, project_id, ANNA)  # back after half an hour: seen again, the same visitor
    assert _totals(_report(env, project_id), "view") == (2, 3)

    _open(env, project_id, photo, ANNA)
    _open(env, project_id, photo, BEN)
    _play(env, project_id, video, ANNA)
    _play(env, project_id, video, ANNA)  # its link renewed
    report = _report(env, project_id)
    assert _totals(report, "open") == (2, 2) and _totals(report, "play") == (1, 1)
    assert {c["asset_id"]: (c["opened"], c["played"]) for c in report["clips"]} == {photo: (2, 0), video: (0, 1)}
    assert [d["day"] for d in report["days"]] == [clock.at.date().isoformat()]


def test_a_signed_in_person_isnt_a_visitor(env, shared, clock):
    client, headers, *_ = env
    project_id, photo, _ = shared
    assert client.get(f"/v1/public/projects/{project_id}", headers={**headers, **ANNA}).status_code == 200
    assert client.get(f"/v1/assets/{photo}", headers={**headers, **ANNA}).status_code == 200
    report = _report(env, project_id)
    assert _totals(report, "view") == (0, 0) and _totals(report, "open") == (0, 0)


def test_a_bot_counts_apart_from_visitors(env, shared, clock):
    project_id, *_ = shared
    _seen(env, project_id, CRAWLER)
    _seen(env, project_id, ANNA)
    report = _report(env, project_id)
    assert _totals(report, "view") == (1, 1) and report["bots"] == 1


def test_where_visitors_came_from_is_a_site_and_nothing_else(env, shared, clock):
    project_id, *_ = shared
    _seen(env, project_id, ANNA, "www.Facebook.com")
    _seen(env, project_id, ANNA, "news.example.org")  # their first says where they came from
    _seen(env, project_id, BEN, "javascript:alert(1)")
    _seen(env, project_id, CRAWLER, "spam.example")
    assert _report(env, project_id)["sources"] == [{"site": "facebook.com", "visitors": 1}]


def test_a_public_librarys_views_opens_and_plays(env, clock):
    client, headers, library_id, *_ = env
    photo = _ingest(env, "Visits/lib.jpg", "image")
    assert client.patch(f"/v1/libraries/{library_id}", json={"is_public": True}, headers=headers).status_code == 200
    try:
        assert client.get(f"/v1/libraries/{library_id}", params={"from": "lumiverb.io"},
                          headers=ANNA).status_code == 200
        assert client.get(f"/v1/assets/{photo}", params={"public_library_id": library_id},
                          headers=ANNA).status_code == 200
        r = client.get(f"/v1/libraries/{library_id}/visits", headers=headers)
        assert r.status_code == 200, r.text
        report = r.json()
        assert _totals(report, "view") == (1, 1) and _totals(report, "open") == (1, 1)
        assert report["sources"] == [{"site": "lumiverb.io", "visitors": 1}]
    finally:
        client.patch(f"/v1/libraries/{library_id}", json={"is_public": False}, headers=headers)


def test_a_project_exported_counts_every_time(env, shared, clock):
    client, headers, *_ = env
    project_id, *_ = shared
    for _ in range(2):
        assert client.get(f"/v1/projects/{project_id}/export", params={"format": "fcpxml"},
                          headers=headers).status_code == 200
    assert _totals(_report(env, project_id), "export") == (1, 2)


def test_the_days_before_today_add_up_the_same_and_who_visited_is_forgotten(env, shared, clock):
    client, headers, *_, tenant_url = env
    project_id, photo, _ = shared
    clock.later(days=-1)
    _seen(env, project_id, ANNA, "facebook.com")
    _seen(env, project_id, BEN)
    _open(env, project_id, photo, ANNA)
    _seen(env, project_id, CRAWLER)
    clock.later(days=1)
    _seen(env, project_id, ANNA)  # today's: a new day, a visitor again
    before = _report(env, project_id)

    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        kept = conn.execute(text("SELECT visitor FROM visits_today WHERE place_id = :p"), {"p": project_id}).scalars()
        assert all(v == visits.BOT or (len(v) == 32 and "anna" not in v) for v in kept), "a hash, never who"
    r = client.post("/v1/upkeep", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["visits"]["added_up"] >= 4
    assert _report(env, project_id) == before
    assert _totals(before, "view") == (3, 3) and before["bots"] == 1
    assert before["sources"] == [{"site": "facebook.com", "visitors": 1}]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM visits_today WHERE day < :d"),
                            {"d": clock.at.date()}).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM visit_salts WHERE day < :d"),
                            {"d": clock.at.date()}).scalar() == 0
    engine.dispose()


def test_only_so_many_days(env, shared, clock):
    project_id, *_ = shared
    clock.later(days=-3)
    _seen(env, project_id, ANNA)
    clock.later(days=3)
    _seen(env, project_id, BEN)
    assert _totals(_report(env, project_id, days=2), "view") == (1, 1)
    assert _totals(_report(env, project_id, days=4), "view") == (2, 2)


def test_a_visit_that_cant_be_counted_never_fails_the_page(env, shared, clock, monkeypatch):
    from src.server.repository.visits import VisitRepository

    project_id, photo, _ = shared

    def broken(*_args, **_kwargs):
        raise RuntimeError("the visits table is gone")

    monkeypatch.setattr(VisitRepository, "count", broken)
    _seen(env, project_id, ANNA)
    _open(env, project_id, photo, ANNA)
    monkeypatch.undo()
    assert _totals(_report(env, project_id), "view") == (0, 0)


def test_who_sees_what(env, shared, clock):
    client, headers, library_id, *_ = env
    project_id, *_ = shared
    _seen(env, project_id, ANNA)

    def person(email: str, role: str) -> dict:
        r = client.post("/v1/users", json={"email": email, "password": "correct-horse-battery-staple",
                                           "role": role}, headers=headers)
        assert r.status_code == 201, r.text
        r = client.post("/v1/auth/login", json={"email": email, "password": "correct-horse-battery-staple"})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    editor = person(f"editor-{project_id[-6:]}@example.com", "editor")
    assert client.get(f"/v1/projects/{project_id}/visits", headers=editor).status_code == 403  # not theirs
    assert client.get("/v1/visits", headers=editor).status_code == 403
    assert client.get(f"/v1/libraries/{library_id}/visits", headers=editor).status_code == 403
    for anonymous in (ANNA, {}):
        assert client.get(f"/v1/projects/{project_id}/visits", headers=anonymous).status_code == 401
        assert client.get("/v1/visits", headers=anonymous).status_code == 401

    r = client.get("/v1/visits", headers=headers)  # an account admin's: every place
    assert r.status_code == 200, r.text
    mine = [p for p in r.json()["places"] if p["place_id"] == project_id]
    assert mine == [{**mine[0], "place": "project", "name": "Beach day", "view": {"visitors": 1, "times": 1}}]


def test_a_project_deleted_for_good_takes_its_visits(env, shared, clock):
    client, headers, *_, tenant_url = env
    project_id, *_ = shared
    _seen(env, project_id, ANNA)
    assert client.delete(f"/v1/projects/{project_id}", headers=headers).status_code == 204  # the trash: kept
    engine = create_engine(tenant_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM visits_today WHERE place_id = :p"),
                            {"p": project_id}).scalar() == 1
    r = client.post("/v1/projects/empty-trash", json={"project_ids": [project_id]}, headers=headers)
    assert r.status_code == 200 and r.json()["deleted"] == 1, r.text
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM visits_today WHERE place_id = :p"),
                            {"p": project_id}).scalar() == 0
    engine.dispose()


def test_lumiverb_visits_says_them(env, shared, clock, monkeypatch):
    from fastapi.testclient import TestClient
    from typer.testing import CliRunner

    from src.client.cli.client import LumiverbClient
    from src.client.cli.commands import visits as command
    from src.client.cli.main import app as cli
    from src.server.api.main import app as api

    _, headers, *_ = env
    project_id, photo, _ = shared
    _seen(env, project_id, ANNA, "facebook.com")
    _seen(env, project_id, BEN)
    clock.later(minutes=31)
    _seen(env, project_id, ANNA)
    _open(env, project_id, photo, ANNA)
    client = LumiverbClient(base_url="http://testserver", token=headers["Authorization"].removeprefix("Bearer "))
    client._client = TestClient(api, headers=headers)
    monkeypatch.setattr(command, "LumiverbClient", lambda: client)
    runner = CliRunner()

    said = runner.invoke(cli, ["visits"], env={"COLUMNS": "200"})
    assert said.exit_code == 0, said.output
    assert "Project: Beach day" in said.output and project_id in said.output
    said = runner.invoke(cli, ["visits", "--project", project_id], env={"COLUMNS": "200"})
    assert said.exit_code == 0, said.output
    assert "3 (2 visitors)" in said.output and photo in said.output and "facebook.com (1)" in said.output
