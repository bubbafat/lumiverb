# ruff: noqa: F811 — pytest fixtures are named as parameters
"""GET /v1/system/health: the Admin page's six rows, gathered from the
scheduler's status, the AI machines, Quickwit, the libraries and the data
disk (the rules alone: tests/test_system_health.py)."""

from __future__ import annotations

import json
from collections import namedtuple
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from src.server.config import get_settings
from src.shared.utils import utcnow
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha

pytestmark = pytest.mark.slow

KEYS = ["website", "processing", "ai", "search", "storage", "disk"]


@pytest.fixture(autouse=True)
def _fresh():
    """Nothing kept from the last test: the cached checks, notes' throttle, the status."""
    from src.server.api.routers import system
    from src.server.search import health as search_health

    system._cache.clear()
    search_health._noted.clear()
    yield
    system._cache.clear()
    search_health._noted.clear()


def _health(env) -> dict:
    client, headers, *_ = env
    r = client.get("/v1/system/health", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _rows(env) -> dict:
    return {row["key"]: row for row in _health(env)["rows"]}


def _status(env, **status) -> None:
    status.setdefault("at", utcnow().isoformat())
    with _db(env) as s:
        s.execute(text("INSERT INTO system_metadata (key, value, updated_at) VALUES ('scheduler.status', :v, now())"
                       " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"v": json.dumps(status)})
        s.commit()


def _forget(env, *keys: str) -> None:
    with _db(env) as s:
        s.execute(text("DELETE FROM system_metadata WHERE key = ANY(:k)"), {"k": list(keys)})
        s.commit()


def _settings(monkeypatch, **env_vars) -> None:
    for k, v in env_vars.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()


@pytest.fixture
def quickwit_on(monkeypatch):
    """Quickwit turned on at a closed port: it isn't answering."""
    _settings(monkeypatch, QUICKWIT_ENABLED="true", QUICKWIT_URL="http://127.0.0.1:9")
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


def test_six_rows_in_order_with_the_worst_overall(env):
    body = _health(env)
    assert [r["key"] for r in body["rows"]] == KEYS
    assert all(r["state"] in ("green", "yellow", "red") and r["reason"] for r in body["rows"])
    order = {"green": 0, "yellow": 1, "red": 2}
    assert body["state"] == max((r["state"] for r in body["rows"]), key=order.__getitem__)
    assert {r["key"]: r["state"] for r in body["rows"]}["website"] == "green"


def test_only_someone_signed_in_sees_it(env):
    client, headers, library_id, *_ = env
    assert client.get("/v1/system/health").status_code == 401
    assert client.get("/v1/system/health", params={"public_library_id": library_id}).status_code == 401


def test_processing_follows_the_scheduler(env):
    _forget(env, "scheduler.status")
    row = _rows(env)["processing"]
    assert row["state"] == "red" and "never" in row["reason"] and row["link"] == "/settings/processing"
    _status(env, at=(utcnow() - timedelta(minutes=2)).isoformat())
    row = _rows(env)["processing"]
    assert row["state"] == "red" and "2 minutes" in row["reason"]
    _status(env)
    assert _rows(env)["processing"]["state"] == "green"


def test_a_clip_that_failed_today_makes_processing_yellow(env):
    from src.server.repository import lineage

    _status(env)
    asset_id = _ingest_with(env, "failing.jpg", _sha())
    with _db(env) as s:
        lineage.record_failure(s, asset_id, "vision", "the model said nothing")
    try:
        row = _rows(env)["processing"]
        assert row["state"] == "yellow" and "1 clip failed" in row["reason"]
        with _db(env) as s:  # a day old: listed in Settings → Processing, not lit here
            s.execute(text("UPDATE artifact_lineage SET failed_at = now() - interval '2 days'"
                           " WHERE asset_id = :a"), {"a": asset_id})
            s.commit()
        from src.server.api.routers import system
        system._cache.clear()
        assert _rows(env)["processing"]["state"] == "green"
    finally:
        with _db(env) as s:
            s.execute(text("DELETE FROM artifact_lineage WHERE asset_id = :a"), {"a": asset_id})
            s.commit()


def test_storage_and_the_libraries_say_which_cant_be_reached(env):
    library_id = env[2]
    _status(env, unreachable=[library_id])
    body = _health(env)
    row = {r["key"]: r for r in body["rows"]}["storage"]
    assert row["state"] == "red" and "Footage" in row["reason"]
    assert row["link"] == f"/libraries/{library_id}/settings"
    assert {lib["library_id"]: lib["reachable"] for lib in body["libraries"]}[library_id] is False
    _status(env, unreachable=[])
    body = _health(env)
    assert {r["key"]: r for r in body["rows"]}["storage"]["state"] == "green"
    assert all(lib["reachable"] is True for lib in body["libraries"])
    # The scheduler stopped: it can't be told.
    _status(env, at=(utcnow() - timedelta(minutes=5)).isoformat(), unreachable=[])
    body = _health(env)
    assert {r["key"]: r for r in body["rows"]}["storage"]["state"] == "yellow"
    assert all(lib["reachable"] is None for lib in body["libraries"])


def test_ai_machines_are_yellow_when_one_doing_a_job_is_offline(env):
    client, headers, *_ = env
    built_in = next(m for m in client.get("/v1/ai", headers=headers).json()["machines"] if m["built_in"])
    r = client.post(f"/v1/ai/machines/{built_in['machine_id']}/status",
                    json={"online": False, "error": "faster-whisper isn't installed"}, headers=headers)
    assert r.status_code == 204, r.text
    row = _rows(env)["ai"]
    assert row["state"] == "yellow" and f"{built_in['name']} is offline" in row["reason"]
    assert row["link"] == "/settings/ai"
    client.post(f"/v1/ai/machines/{built_in['machine_id']}/status", json={"online": True, "models": ["small"]},
                headers=headers)
    assert _rows(env)["ai"]["state"] == "green"


def test_search_is_yellow_with_quickwit_off(env):
    row = _rows(env)["search"]  # the suite runs with Quickwit off
    assert row["state"] == "yellow" and "Quickwit is off" in row["reason"]


def test_a_search_quickwit_failed_is_noted_and_postgres_answers(env, quickwit_on):
    client, headers, library_id, *_ = env
    _forget(env, "search.fallback", "search.failure")
    r = client.get("/v1/query", params=[("f", f"library:{library_id}"), ("f", "query:zebra")], headers=headers)
    assert r.status_code == 200 and r.json()["search_source"] == "postgres_fallback"
    with _db(env) as s:
        noted = json.loads(s.execute(text("SELECT value FROM system_metadata WHERE key = 'search.fallback'")).scalar())
    assert "Quickwit" in noted["reason"]
    row = _rows(env)["search"]
    assert row["state"] == "yellow" and "isn't answering" in row["reason"]


def test_search_is_red_when_quickwit_fails_and_the_fallback_is_off(env, quickwit_on, monkeypatch):
    client, headers, library_id, *_ = env
    _forget(env, "search.fallback", "search.failure")
    _settings(monkeypatch, QUICKWIT_FALLBACK_TO_POSTGRES="false")
    r = client.get("/v1/query", params=[("f", f"library:{library_id}"), ("f", "query:zebra")], headers=headers)
    assert r.status_code == 200 and r.json()["items"] == []
    row = _rows(env)["search"]
    assert row["state"] == "red"
    with _db(env) as s:
        assert s.execute(text("SELECT 1 FROM system_metadata WHERE key = 'search.failure'")).scalar() == 1
    _forget(env, "search.failure")


def test_search_is_yellow_when_the_accounts_index_is_missing(env, quickwit_on):
    with patch("src.server.search.quickwit_client.requests.get", return_value=MagicMock(status_code=404)):
        row = _rows(env)["search"]
    assert row["state"] == "yellow" and "index is missing" in row["reason"]


def test_search_is_green_when_quickwit_answers_and_counts_whats_not_indexed(env, quickwit_on):
    _forget(env, "search.fallback", "search.failure")
    with patch("src.server.search.quickwit_client.requests.get", return_value=MagicMock(status_code=200)):
        row = _rows(env)["search"]
    with _db(env) as s:
        unsynced = s.execute(text("SELECT count(*) FROM active_assets WHERE search_synced_at IS NULL")).scalar()
    assert row["state"] == ("green" if unsynced < 100 else "yellow")


def test_disk_follows_free_space_on_the_data_disk(env):
    Usage = namedtuple("Usage", "total used free")
    with patch("src.server.api.routers.system.shutil.disk_usage", return_value=Usage(1000, 970, 30)):
        assert _rows(env)["disk"]["state"] == "red"
    with patch("src.server.api.routers.system.shutil.disk_usage", return_value=Usage(1000, 900, 100)):
        assert _rows(env)["disk"]["state"] == "yellow"
    with patch("src.server.api.routers.system.shutil.disk_usage", side_effect=FileNotFoundError("gone")):
        row = _rows(env)["disk"]
    assert row["state"] == "yellow" and "gone" in row["reason"]


def test_a_row_that_cant_be_checked_says_so_without_taking_the_page_down(env):
    with patch("src.server.api.routers.system.h.ai_row", side_effect=RuntimeError("boom")):
        row = _rows(env)["ai"]
    assert row["state"] == "yellow" and "boom" in row["reason"]


def test_a_missing_index_made_by_an_inline_sync_indexes_everything_again(env):
    """Before, an inline sync made the missing index and nothing else: every
    clip synced before it was deleted stayed out of search for good."""
    from src.server.models.tenant import Asset
    from src.server.search.sync import try_sync_asset

    tenant_id = env[4]
    a = _ingest_with(env, "kept.jpg", _sha())
    b = _ingest_with(env, "edited.jpg", _sha())
    with _db(env) as s:
        s.execute(text("UPDATE assets SET search_synced_at = now()"))
        s.commit()
        qw = MagicMock()
        qw.ensure_tenant_index.return_value = True  # it was missing: made just now
        assert try_sync_asset(s, s.get(Asset, b), None, tenant_id=tenant_id, quickwit=qw)
        synced = dict(s.execute(text("SELECT asset_id, search_synced_at IS NOT NULL FROM assets"
                                     " WHERE asset_id = ANY(:ids)"), {"ids": [a, b]}).all())
    assert synced == {a: False, b: True}  # the sweep puts a back in


def test_noting_is_throttled_per_kind(env):
    from src.server.search import health as search_health

    _forget(env, "search.fallback")
    with _db(env) as s:
        search_health.note(s, search_health.FALLBACK, "first")
        search_health.note(s, search_health.FALLBACK, "second")  # within the minute: not written
        when, why = search_health.last(s, search_health.FALLBACK)
    assert why == "first" and utcnow() - when < timedelta(minutes=1)
    _forget(env, "search.fallback")



def test_a_failed_query_in_one_row_doesnt_spoil_the_next(env):
    """Postgres aborts the transaction on an error: without a rollback the
    rows after it failed too, and Storage read no libraries as green."""
    def broken(session, *a, **kw):
        session.execute(text("SELECT * FROM no_such_table"))

    _status(env)
    with patch("src.server.api.routers.system._processing", side_effect=broken):
        rows = _rows(env)
    assert rows["processing"]["state"] == "yellow" and "Couldn't be checked" in rows["processing"]["reason"]
    assert "Couldn't be checked" not in rows["search"]["reason"]
    assert rows["storage"]["reason"] != "No libraries." and "Couldn't be checked" not in rows["storage"]["reason"]


def test_a_scheduler_status_that_cant_be_read_isnt_called_never_run(env):
    with patch("src.server.api.routers.producers.scheduler_status", side_effect=RuntimeError("bad json")):
        row = _rows(env)["processing"]
    assert row["state"] == "yellow" and "Couldn't read" in row["reason"]
