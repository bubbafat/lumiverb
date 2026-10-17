# ruff: noqa: F811 — pytest fixtures are named as parameters
"""GET /v1/system/health: the Admin page's seven rows, gathered from the
scheduler's status, the AI machines, the clips not searchable yet, the
libraries, the data disk and the backups (the rules alone:
tests/test_system_health.py). The disk and the backups are the install's, so
a server admin sees them (signed in, as the Admin page is) and an account's
admin key doesn't."""

from __future__ import annotations

import json
from collections import namedtuple
from datetime import timedelta
from unittest.mock import patch

import bcrypt
import pytest
from sqlalchemy import text

from src.shared.utils import utcnow
from tests.clips import make_clip
from tests.test_analysis_proxy_api import env  # noqa: F401 — the shared server fixture
from tests.test_lineage_api import _db, _ingest_with, _sha

pytestmark = pytest.mark.slow

KEYS = ["website", "processing", "ai", "search", "storage", "disk", "backups"]
_signed_in: dict[str, dict[str, str]] = {}


def _server_admin(env) -> dict[str, str]:
    """The install's admin in the account, signed in (once a module)."""
    from src.server.database import get_control_session
    from src.server.repository.control_plane import UserRepository

    client, _, _, _, tenant_id, _ = env
    if tenant_id not in _signed_in:
        password = "correct horse battery"
        with get_control_session() as session:
            UserRepository(session).create(tenant_id, "server-admin@example.com",
                                           bcrypt.hashpw(password.encode(), bcrypt.gensalt(4)).decode(),
                                           role="admin", server_admin=True)
        r = client.post("/v1/auth/login", json={"email": "server-admin@example.com", "password": password})
        assert r.status_code == 200, r.text
        _signed_in[tenant_id] = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return _signed_in[tenant_id]


@pytest.fixture(autouse=True)
def _fresh():
    """Nothing kept from the last test: the cached checks."""
    from src.server.api.routers import system

    system._cache.clear()
    yield
    system._cache.clear()


def _health(env) -> dict:
    client, *_ = env
    r = client.get("/v1/system/health", headers=_server_admin(env))
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


def test_seven_rows_in_order_with_the_worst_overall(env):
    body = _health(env)
    assert [r["key"] for r in body["rows"]] == KEYS
    client, headers, *_ = env
    keyed = client.get("/v1/system/health", headers=headers).json()["rows"]
    assert [r["key"] for r in keyed] == KEYS[:5], "an account admin's key: the account's rows"
    assert all(r["state"] in ("green", "yellow", "red") and r["reason"] for r in body["rows"])
    order = {"green": 0, "yellow": 1, "red": 2}
    assert body["state"] == max((r["state"] for r in body["rows"]), key=order.__getitem__)
    assert {r["key"]: r["state"] for r in body["rows"]}["website"] == "green"


def test_it_says_which_lumiverb_is_running(env):
    from src.server.version import running

    assert _health(env)["version"] == running() != ""  # this checkout: what git says


def test_only_someone_signed_in_sees_it(env):
    client, headers, library_id, *_ = env
    assert client.get("/v1/system/health").status_code == 401
    assert client.get("/v1/system/health", params={"public_library_id": library_id}).status_code == 401


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_only_admins_see_it(env, role):
    """The Admin page's: nothing in it is theirs to fix."""
    client, headers, *_ = env
    made = client.post("/v1/keys", json={"label": f"{role}-health", "role": role}, headers=headers)
    assert made.status_code == 200, made.text
    theirs = {"Authorization": f"Bearer {made.json()['plaintext']}"}
    assert client.get("/v1/system/health", headers=theirs).status_code == 403


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


def _seen(minutes: float | None = 0, away: float | None = None, checked: bool = True) -> dict:
    t = utcnow()
    return {"seen_at": (t - timedelta(minutes=minutes)).isoformat() if minutes is not None else None,
            "away_since": (t - timedelta(minutes=away)).isoformat() if away is not None else None,
            "checked": checked}


def _storage_and_dots(env) -> tuple[dict, dict]:
    body = _health(env)
    return ({r["key"]: r for r in body["rows"]}["storage"],
            {lib["library_id"]: lib for lib in body["libraries"]})


def test_storage_and_the_libraries_say_which_cant_be_reached(env):
    library_id = env[2]
    _status(env, storage={library_id: _seen(120, away=90)})
    row, dots = _storage_and_dots(env)
    assert row["state"] == "red" and row["reason"] == "Can't reach Footage. Last seen 2 hours ago."
    assert row["link"] == f"/libraries/{library_id}/settings"
    assert dots[library_id]["reachable"] is False and dots[library_id]["seen_at"]
    _status(env, storage={library_id: _seen()})
    row, dots = _storage_and_dots(env)
    assert row["state"] == "green" and all(d["reachable"] is True for d in dots.values())
    # The scheduler stopped: it can't be told.
    _status(env, at=(utcnow() - timedelta(minutes=5)).isoformat(), storage={library_id: _seen()})
    row, dots = _storage_and_dots(env)
    assert row["state"] == "yellow" and all(d["reachable"] is None for d in dots.values())


def test_storage_after_a_restart_isnt_red_until_its_looked_at(env):
    # The bug (Oct 9): restarted with Scans paused, every library read "Can't reach".
    library_id = env[2]
    _status(env, storage={library_id: _seen(120, checked=False)})
    row, dots = _storage_and_dots(env)
    assert row["state"] == "yellow" and row["reason"] == "Not checked yet. Footage last seen 2 hours ago."
    assert dots[library_id]["reachable"] is None
    _status(env, storage={})
    row, _ = _storage_and_dots(env)
    assert row["state"] == "yellow" and row["reason"] == "Not checked yet. Footage never seen."


def test_storage_while_scans_are_paused_is_what_the_latest_look_found(env):
    # Robert, Oct 12: pausing Scans stops only looking for new and changed
    # files; the scheduler still looks at each library's storage itself.
    client, headers, library_id, *_ = env
    try:
        r = client.post("/v1/libraries/scans/pause", json={"library_ids": [library_id]}, headers=headers)
        assert r.status_code == 204, r.text
        _status(env, storage={library_id: _seen(120, away=1)})
        row, dots = _storage_and_dots(env)
        assert row["state"] == "red" and row["reason"] == "Can't reach Footage. Last seen 2 hours ago."
        assert dots[library_id]["reachable"] is False
        _status(env, storage={library_id: _seen()})
        row, dots = _storage_and_dots(env)
        assert row["state"] == "green" and dots[library_id]["reachable"] is True
    finally:
        client.post("/v1/producers/all/resume", json={"scope": "work"}, headers=headers)


def test_storage_while_all_processing_is_paused_says_when_each_was_last_seen(env):
    # Pause all: nothing touches the drives, the scheduler's look included.
    client, headers, library_id, *_ = env
    try:
        assert client.post("/v1/producers/all/pause", json={"scope": "work"}, headers=headers).status_code == 204
        _status(env, storage={library_id: _seen(120, away=1)})
        row, dots = _storage_and_dots(env)
        assert row["state"] == "yellow"
        assert row["reason"] == "All processing paused, so storage isn't checked. Footage last seen 2 hours ago."
        assert dots[library_id]["reachable"] is None
    finally:
        client.post("/v1/producers/all/resume", json={"scope": "work"}, headers=headers)


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


def _all_searchable(env) -> None:
    """Every clip's search row written: the search sync's sweep, as upkeep runs it."""
    from src.server.search.sync import run_search_sync_sweep

    with _db(env) as s:
        assert run_search_sync_sweep(s)["failed"] == 0
        assert s.execute(text("SELECT count(*) FROM active_assets WHERE search_synced_at IS NULL")).scalar() == 0


def test_search_is_green_when_every_clip_is_searchable(env):
    _all_searchable(env)
    row = _rows(env)["search"]
    assert row["state"] == "green" and row["reason"] == "Every clip is searchable."


def test_search_is_yellow_from_the_threshold_of_clips_in_sight_not_searchable_yet(env):
    from src.server import system_health as h
    from src.server.api.routers import system

    client, headers, library_id, *_ = env
    _all_searchable(env)
    clips = [make_clip(env[-1], library_id, f"unsearched/{i:03}.jpg") for i in range(h.UNSYNCED_YELLOW)]
    try:
        r = client.request("DELETE", "/v1/assets", json={"asset_ids": clips[:1], "reason": "user"}, headers=headers)
        assert r.status_code == 200 and r.json()["trashed"] == clips[:1], r.text
        assert _rows(env)["search"]["state"] == "green"  # the one in the trash isn't counted
        make_clip(env[-1], library_id, "unsearched/one-more.jpg")
        system._cache.clear()
        row = _rows(env)["search"]
        assert row["state"] == "yellow"
        assert row["reason"] == f"{h.UNSYNCED_YELLOW} clips aren't searchable yet: upkeep adds them within minutes."
    finally:
        _all_searchable(env)


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


SRT = "1\n00:00:00,000 --> 00:00:01,500\nthe ferry leaves at noon\n"


def test_the_sweep_writes_the_transcripts_search_doesnt_have(env):
    """Before, the sweep never indexed transcripts: one went back in only when
    submitted again. Now a transcript whose segments were never written, or
    were written before it was transcribed again, is written by the sweep."""
    from src.server.search.sync import run_search_sync_sweep

    never = _ingest_with(env, "ferry.mov", _sha(), media_type="video")
    again = _ingest_with(env, "dock.mov", _sha(), media_type="video")
    with _db(env) as s:
        s.execute(text("UPDATE assets SET transcript_srt = :t, transcribed_at = now() - interval '1 hour',"
                       " transcript_synced_at = CASE WHEN asset_id = :again THEN now() - interval '2 hours' END"
                       " WHERE asset_id = ANY(:ids)"), {"t": SRT, "again": again, "ids": [never, again]})
        s.commit()
        result = run_search_sync_sweep(s)
        assert result["transcripts_synced"] >= 2 and result["transcripts_failed"] == 0
        segments = s.execute(text("SELECT asset_id, text FROM transcript_segments WHERE asset_id = ANY(:ids)"),
                             {"ids": [never, again]}).all()
        assert sorted(tuple(r) for r in segments) == sorted([(never, "the ferry leaves at noon"),
                                                             (again, "the ferry leaves at noon")])
        # Synced since it was transcribed: the next sweep leaves them.
        assert s.execute(text("SELECT count(*) FROM assets WHERE asset_id = ANY(:ids)"
                              " AND transcript_synced_at > transcribed_at"), {"ids": [never, again]}).scalar() == 2


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


def test_processing_follows_the_pause_switches(env):
    """GET /v1/producers/queue's state: running, partly (yellow), paused (red)."""
    client, headers, library_id, *_ = env
    _status(env)
    assert _rows(env)["processing"]["reason"] == "Running."
    try:
        r = client.post("/v1/libraries/scans/pause", json={"library_ids": [library_id]}, headers=headers)
        assert r.status_code == 204, r.text
        row = _rows(env)["processing"]
        assert row["state"] == "yellow" and row["reason"].startswith("Partly paused: Scans of Footage"), row
        assert client.post("/v1/producers/upkeep/pause", json={"scope": "work"}, headers=headers).status_code == 204
        assert _rows(env)["processing"]["reason"].startswith("Partly paused: Scans of Footage and Upkeep")
        assert client.post("/v1/producers/all/pause", json={"scope": "work"}, headers=headers).status_code == 204
        row = _rows(env)["processing"]
        assert row["state"] == "red" and row["reason"].startswith("Paused")
    finally:
        assert client.post("/v1/producers/all/resume", json={"scope": "work"}, headers=headers).status_code == 204
    assert _rows(env)["processing"]["state"] == "green"
